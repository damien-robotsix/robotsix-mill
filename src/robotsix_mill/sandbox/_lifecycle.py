"""Sandbox lifecycle: workspace discovery, orphan reaping, cache pruning."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from ..config import Settings
from ._fetch import _list_sandbox_containers, _parse_docker_started_at

log = logging.getLogger("robotsix_mill.sandbox._lifecycle")

# Package cache: on DISK, not in the tmpfs -----------------------------------
#
# ``HOME=/tmp`` and ``/tmp`` is a tmpfs, i.e. RAM charged to the sandbox's own
# memory cgroup. So every byte ``uv``/``pip`` cached under ``$HOME/.cache`` ate
# the container's memory limit. Since the test gate started installing the
# project (``_maybe_install_prefix``), and especially since the ``uv sync``
# path landed, that is the whole dependency tree: measured 2026-08-02 on the
# deploy box, 625 MB of ``/tmp/.cache`` in one sandbox and another pinned at
# 1022 MiB against its 1 GiB cap. tmpfs pages can't be reclaimed under
# pressure either — they go to swap — which is how a few sandboxes put the
# host into swap thrash.
#
# Pointing the caches at a volume subpath fixes the memory charge and, because
# the cache then shares a filesystem with the repo mount, lets uv hardlink into
# ``.venv`` instead of copying. Reuse across sandboxes also drops the repeated
# download + unpack that showed up as ~100% CPU per container.
#
# The mount is subpath-scoped exactly like the repo mount, so the management
# plane (``mill.db``, memory ledgers, other tickets' workspaces) stays
# unreachable. What it does add is a channel BETWEEN sandboxes: a poisoned
# wheel written by one is visible to the next. Acceptable here — sandboxes run
# first-party repo code whose author could equally well edit the repo — and
# ``sandbox_package_cache=false`` turns it off.
_CACHE_SUBDIR = "_sandbox_cache"


def live_sandbox_workspace_paths() -> set[Path]:
    """Workspace dirs currently mounted into a RUNNING sandbox.

    Mill mounts each workspace by volume-subpath, so a sandbox's mount
    *destination* is the ``/data/<board>/workspaces/<ticket>/repo`` (or
    ``…/repos/<name>``) path; this maps those back to workspace roots.

    Consumed by the data-dir GC, which reclaims ``.venv`` from parked
    tickets: a parked ticket normally has no sandbox at all, so this set
    is usually disjoint from the prune candidates. It exists to close the
    resume race — a ticket un-parked between the GC's DB read and its
    ``rmtree`` would otherwise have the venv deleted out from under a
    live ``uv sync``, failing that ticket's gate for no reason.

    Only RUNNING containers count, unlike :func:`_list_sandbox_containers`
    (which lists all states so the reaper can sweep ``Created`` husks): a
    container that is not running holds nothing open.

    Best-effort — an empty set on any Docker CLI failure, degrading to an
    unguarded prune rather than skipping reclamation entirely.
    """
    try:
        listing = subprocess.run(
            ["docker", "ps", "--no-trunc", "--format", "{{.ID}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if listing.returncode != 0:
            return set()
        ids = [line.strip() for line in listing.stdout.splitlines() if line.strip()]
        if not ids:
            return set()
        inspected = subprocess.run(
            ["docker", "inspect", "--format", "{{json .Mounts}}", *ids],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if inspected.returncode != 0:
            return set()
    except OSError, subprocess.SubprocessError:
        return set()

    paths: set[Path] = set()
    for line in inspected.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            mounts = json.loads(line)
        except ValueError:
            continue
        for mount in mounts or ():
            dest = Path(str(mount.get("Destination", "")))
            if dest.name == "repo":
                paths.add(dest.parent)
            elif dest.parent.name == "repos":
                paths.add(dest.parent.parent)
    return paths


def _container_age_exceeds(cid: str, max_age_seconds: int) -> bool:
    """True when container ``cid``'s uptime exceeds ``max_age_seconds``.

    Returns ``False`` on any inspect/parse failure so an unreadable
    container is left alone — the startup reaper (which ignores age) is
    the guaranteed backstop for those.
    """
    try:
        ins = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.StartedAt}}", cid],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError, subprocess.SubprocessError:
        return False
    if ins.returncode != 0:
        return False
    started = _parse_docker_started_at(ins.stdout)
    if started is None:
        return False
    return (datetime.now(UTC) - started).total_seconds() > max_age_seconds


def reap_orphan_sandboxes(*, max_age_seconds: int | None = None) -> int:
    """Force-remove leaked sandbox containers; return the count removed.

    Sandbox containers (``mill-sbx-*`` from :func:`run`, ``mill-fetch-*``
    from :func:`fetch`) are disposable: they are created with ``--rm`` and
    their only deadline is the *parent* ``subprocess.run(timeout=...)``.
    If the mill process dies or is restarted while a sandbox is mid-run,
    the ``except TimeoutExpired`` cleanup never executes and ``--rm`` never
    fires (it triggers on container *exit*, which a runaway command never
    reaches) — leaving the container running forever, potentially pegging a
    CPU core (observed: a 3.5-day runaway that saturated the API).

    ``max_age_seconds=None`` removes **all** matching containers — correct
    at process startup, where any present are by definition orphans from
    before this process began (nothing has launched a sandbox yet).  A
    positive value removes only containers whose uptime exceeds it — used
    by the periodic reaper, where a legitimate sandbox never outlives
    ``command_timeout``.

    Best-effort: never raises.  A missing/slow/erroring Docker CLI must not
    crash lifespan startup or the worker poll loop, so failures are
    swallowed and reported as ``0`` reaped.
    """
    candidates = _list_sandbox_containers()
    if max_age_seconds is not None:
        candidates = [
            (cid, name)
            for cid, name in candidates
            if _container_age_exceeds(cid, max_age_seconds)
        ]

    reaped = 0
    for cid, name in candidates:
        try:
            rm = subprocess.run(
                ["docker", "rm", "-f", cid],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except OSError, subprocess.SubprocessError:
            continue
        if rm.returncode == 0:
            reaped += 1
            log.warning("reaped orphan sandbox container %s", name)
    return reaped


def _dir_size_bytes(path: Path) -> int:
    """Total size of *path*, ignoring anything that vanishes mid-walk."""
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def prune_package_cache(settings: Settings) -> int:
    """Drop the shared sandbox package cache when it outgrows its budget;
    return the bytes freed (0 when nothing was done).

    uv and pip caches only ever grow, and this one lives on the data
    volume — the same volume whose exhaustion has taken the mill down
    before. It is pure cache, so the cheap correct answer is to delete it
    wholesale and let the next sandbox refill it.

    Only runs when NO sandbox container is alive: deleting entries under a
    concurrent ``uv sync`` would fail that ticket's gate for no reason. The
    caller (the sandbox-reaper pass) retries every few minutes, so skipping
    a busy moment costs nothing.

    Best-effort: never raises — a failed prune must not kill the poll loop.
    """
    if not settings.sandbox_package_cache:
        return 0
    cache_dir = Path(settings.data_dir).resolve() / _CACHE_SUBDIR
    if not cache_dir.is_dir():
        return 0
    budget = max(0, settings.sandbox_package_cache_max_mb) * 1024 * 1024
    if budget <= 0:
        return 0
    try:
        size = _dir_size_bytes(cache_dir)
    except OSError:
        return 0
    if size <= budget:
        return 0
    if _list_sandbox_containers():
        log.info(
            "sandbox package cache is %d MiB (budget %d MiB) but sandboxes "
            "are live; deferring prune",
            size // (1024 * 1024),
            settings.sandbox_package_cache_max_mb,
        )
        return 0

    try:
        shutil.rmtree(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.warning("sandbox package cache prune failed", exc_info=True)
        return 0
    log.info(
        "pruned sandbox package cache: freed %d MiB (budget %d MiB)",
        size // (1024 * 1024),
        settings.sandbox_package_cache_max_mb,
    )
    return size
