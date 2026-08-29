"""Command execution isolation — always containerized.

The implement agent's ``run_command`` tool and the stage's test command
run **attacker-influenceable** code (ticket text and cloned repo content
steer the LLM). They must never run in the mill process or on the host.

There is intentionally **no in-process / "local" mode** — that was a
foot-gun that let an agent edit the host and recursively re-invoke the
pipeline. Every command runs in a fresh, disposable sibling container:
``--network none``, ``--rm``, non-root, read-only root with a tmpfs
``/tmp``, pids/memory capped, and **only the ticket's repo reachable**.
Tests fake :func:`run` (the seam) rather than relying on an unsafe mode.

Sibling-container mount caveat: when mill talks to the host Docker
daemon over the mounted socket, ``-v`` paths resolve on the **host**,
not inside the mill container. The sandbox therefore exposes **only
the ticket's own ``repo/`` sub-tree** (at its real path so ``-w`` and
absolute refs line up) — never the data-dir root. ``mill.db``, the
agent memory ledgers, and other tickets' workspaces are NOT reachable:
a ticket's tests/commands cannot read or corrupt the management plane.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ..config import Settings
from ..config.repo_settings import (
    load_extra_sandbox_packages as load_extra_sandbox_packages,
)
from ._slots import (
    DEFAULT_RANK as DEFAULT_RANK,  # re-exported: callers set/read the rank here
)
from ._slots import (
    PrioritySlots,
    current_rank,
)
from ._slots import (
    sandbox_rank as sandbox_rank,
)

_OUT_CAP = 8000

# Name prefixes of the disposable sibling containers this module spawns:
# ``run()`` uses ``mill-sbx-*`` and ``fetch()`` uses ``mill-fetch-*``.
_SANDBOX_CONTAINER_PREFIXES = ("mill-sbx-", "mill-fetch-")

log = logging.getLogger("robotsix_mill.sandbox")


class SandboxError(RuntimeError):
    """Infrastructure failure (no Docker, daemon/image error) — distinct
    from the command itself exiting non-zero.
    """


# Hard ceiling on live sandbox containers -----------------------------------
#
# ``max_global_concurrency`` used to be applied ONLY around the board
# consumer's ``process_ticket`` call (``runtime/worker/core.py``), so it
# bounded ticket *stages* and nothing else. Every other sandbox spawner runs
# outside that semaphore: the ~20 per-repo periodic passes (audit, test-gap,
# health, ...), the meta-agent, the diagnostic pass, and refine's warnings
# collection. So the live sandbox count routinely exceeded the cap — observed
# 2026-08-02 with the cap set to 1: three ``mill-sbx-*`` containers, one of
# them a ``test_gap_workspace`` periodic pass, on a box already swapping.
#
# Sandboxes are what actually costs memory, so the ceiling belongs where they
# are created rather than at one of several call paths into it. A module-level
# semaphore here is unmissable: every spawner goes through ``run()``.
#
# ``threading`` (not ``asyncio``) because ``run()`` is synchronous and called
# from worker threads — stage handlers offload to threads precisely because
# the agent SDK is sync. Blocking here adds no new event-loop risk: the
# ``subprocess.run`` below already blocks its caller for up to the op timeout.
#
# The pool is priority-aware rather than FIFO. Sharing one ceiling between
# ticket stages and the ~20 periodic passes means a plain semaphore lets a
# ``test_gap_workspace`` pass take the last slot ahead of a flagged ticket
# that already won both its board queue and the global stage gate. See
# ``_slots.PrioritySlots``; the rank comes from the ``sandbox_rank``
# context variable, which the board consumer sets around each stage run.
_slot_lock = threading.Lock()
_slot_pool: PrioritySlots | None = None
_slot_cap = 0


def _slot_semaphore(cap: int) -> PrioritySlots:
    """Return the process-wide sandbox-slot pool, sized to *cap*.

    Rebuilt when *cap* changes so a settings override (and tests) take
    effect; in-flight holders keep the object they acquired, so the
    swap can't over-release.
    """
    global _slot_pool, _slot_cap
    with _slot_lock:
        if _slot_pool is None or _slot_cap != cap:
            _slot_pool = PrioritySlots(cap)
            _slot_cap = cap
        return _slot_pool


@contextmanager
def _sandbox_slot(settings: Settings) -> Iterator[None]:
    """Hold one of ``max_global_concurrency`` sandbox slots for the block.

    Waiters are admitted best-rank-first (see :data:`sandbox_rank`), FIFO
    within a rank.

    Raises :class:`SandboxError` if no slot frees up within
    ``sandbox_slot_timeout`` — a bounded wait so a leaked slot surfaces
    as a stage error instead of hanging a worker thread forever.
    """
    cap = max(1, settings.max_global_concurrency)
    pool = _slot_semaphore(cap)
    timeout = max(1, settings.sandbox_slot_timeout)
    rank = current_rank()
    if pool.in_use() >= cap:
        # Queueing is normal under load, but it is also the signal that the
        # cap is what's slowing the board down — worth a line in the log.
        log.info("sandbox cap (%d) saturated; waiting for a slot at rank %s", cap, rank)
    if not pool.acquire(rank, timeout):
        raise SandboxError(
            f"no sandbox slot free after {timeout}s "
            f"(cap={cap}); too many concurrent sandboxes"
        )
    try:
        yield
    finally:
        pool.release()


# Deploy-mode (central-deploy) helpers --------------------------------------
#
# Under central-deploy the mill talks to a REMOTE Docker daemon through the
# hardened socket-proxy (``DOCKER_HOST``). central-deploy's contract ignores
# the dev stack's compose-declared ``networks:`` block and does not wire the
# data volume into the sandbox config, so two things the dev stack handles
# statically must be established at runtime:
#
#   * the internal egress network + ``sandbox-proxy`` attachment, and
#   * the host-side mount backing ``MILL_DATA_DIR`` (named volume or bind).
#
# Every helper below is best-effort and never raises, and the call sites are
# gated on ``DOCKER_HOST`` being set so the dev stack path is unchanged.


def ensure_sandbox_network(settings: Settings) -> bool:
    """Create the internal egress network and attach the egress proxy.

    Returns ``True`` when the ``sandbox-proxy`` container is attached to the
    network — including the no-op case where no proxy is configured (then
    sandboxes run ``--network none`` and there is nothing to do). Returns
    ``False`` on any Docker CLI failure.

    Idempotent: an already-existing network and an already-attached proxy
    are both treated as success. Best-effort — never raises; failures are
    logged and surfaced via the return value so the caller can continue.
    """
    if not settings.sandbox_proxy_url:
        return True
    net = settings.sandbox_network
    # Create the internal network (idempotent — "already exists" is success).
    try:
        create = subprocess.run(
            ["docker", "network", "create", "--internal", net],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError, subprocess.SubprocessError:
        log.warning("ensure_sandbox_network: `docker network create %s` failed", net)
        return False
    if create.returncode != 0 and "already exists" not in create.stderr:
        log.warning(
            "ensure_sandbox_network: `docker network create %s` failed: %s",
            net,
            create.stderr.strip()[:200],
        )
        return False
    # Attach the egress proxy (idempotent — already-connected is success).
    try:
        connect = subprocess.run(
            ["docker", "network", "connect", net, "sandbox-proxy"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError, subprocess.SubprocessError:
        log.warning(
            "ensure_sandbox_network: `docker network connect %s sandbox-proxy` failed",
            net,
        )
        return False
    if connect.returncode == 0 or "already exists in network" in connect.stderr:
        log.info("ensure_sandbox_network: %s ready (proxy attached)", net)
        return True
    log.warning(
        "ensure_sandbox_network: `docker network connect %s sandbox-proxy` failed: %s",
        net,
        connect.stderr.strip()[:200],
    )
    return False


def resolve_data_volume(settings: Settings) -> None:
    """Resolve the host-side mount backing ``MILL_DATA_DIR`` and record it.

    The sandbox mounts a ticket's repo subtree using a path/volume the HOST
    daemon understands (``-v``/``--mount`` resolve on the host, not inside
    the mill container). The dev stack wires this statically; under
    central-deploy the mill must discover it by inspecting its OWN container.

    Inspects ``<hostname>`` (the container id) for the mount whose
    ``Destination`` equals the resolved ``settings.data_dir`` and mutates
    *settings* in place:

    * named volume → set ``data_volume`` to the volume name and clear
      ``sandbox_data_mount``;
    * bind mount → set ``sandbox_data_mount`` to the host source path.

    Best-effort — never raises; any failure leaves *settings* unchanged.
    """
    cid = socket.gethostname()
    try:
        ins = subprocess.run(
            ["docker", "inspect", "--format", "{{json .Mounts}}", cid],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError, subprocess.SubprocessError:
        log.warning("resolve_data_volume: `docker inspect %s` failed to run", cid)
        return
    if ins.returncode != 0:
        log.warning(
            "resolve_data_volume: `docker inspect %s` failed: %s",
            cid,
            ins.stderr.strip()[:200],
        )
        return
    try:
        mounts = json.loads(ins.stdout)
    except ValueError, TypeError:
        log.warning("resolve_data_volume: could not parse docker inspect Mounts JSON")
        return
    if not isinstance(mounts, list):
        return
    target = str(Path(settings.data_dir).resolve())
    for m in mounts:
        if not isinstance(m, dict) or m.get("Destination") != target:
            continue
        mtype = m.get("Type")
        if mtype == "volume" and m.get("Name"):
            settings.data_volume = m["Name"]
            settings.sandbox_data_mount = None
            log.info("resolve_data_volume: data volume resolved to %s", m["Name"])
        elif mtype == "bind" and m.get("Source"):
            settings.sandbox_data_mount = m["Source"]
            log.info(
                "resolve_data_volume: data bind source resolved to %s", m["Source"]
            )
        return
    log.warning(
        "resolve_data_volume: no mount matched data_dir %s; leaving config unchanged",
        target,
    )


def _truncate(out: str) -> str:
    return out[:_OUT_CAP]


def _repo_mount(repo_dir: Path, settings: Settings) -> list[str]:
    """Mount ONLY this ticket's repo sub-tree into the sandbox — never
    the data-dir root (which holds ``mill.db``, the memory ledgers and
    every other ticket's workspace). Target = the repo's real path so
    ``-w`` and any absolute path in the repo still resolve.
    """
    # Resolve both to absolute up-front. Docker's `-w` and the volume
    # target both REQUIRE absolute paths; the default settings.data_dir
    # is the relative Path(".data"), so without resolution the
    # downstream `-w str(repo_dir)` emits "Path .data/... is invalid,
    # it needs to be an absolute path" (seen as ticket-blocking error
    # on bc-check-agent-add-done-... 2026-05-29 08:00).
    repo_dir = Path(repo_dir).resolve()
    data_dir = Path(settings.data_dir).resolve()
    try:
        rel = repo_dir.relative_to(data_dir)
    except ValueError as e:
        raise SandboxError(
            f"repo_dir {repo_dir} is not under data_dir {data_dir}; refusing to mount"
        ) from e
    if rel == Path("."):
        raise SandboxError("refusing to mount the data-dir root as repo")
    target = str(repo_dir)
    if settings.sandbox_data_mount:
        # bind case: resolve the repo's host path (data_mount + rel).
        # The host path is meaningful to DOCKER (which runs on the
        # host) for the bind mount — checking its existence from
        # INSIDE the mill container is broken: the container's fs only
        # has the data dir at the container path (e.g. /data), not at
        # the host's absolute path, so Path(host_src).exists() is
        # ALWAYS False here (false negative -> every sandbox call
        # fails with "repo not cloned"). Verify the container-visible
        # path instead — same as the named-volume branch below.
        host_src = Path(settings.sandbox_data_mount) / rel
        if not repo_dir.exists():
            raise SandboxError(
                f"repo directory does not exist: {repo_dir} — "
                "the repository has not been cloned yet"
            )
        return ["-v", f"{host_src}:{target}"]
    # named-volume case: the volume exists, but the repo subdirectory
    # on the host must also exist so that Docker can bind-mount the
    # volume subpath (otherwise Docker fails with a generic error).
    if not repo_dir.exists():
        raise SandboxError(
            f"repo directory does not exist: {repo_dir} — "
            "the repository has not been cloned yet"
        )
    return [
        "--mount",
        (
            f"type=volume,src={settings.data_volume},dst={target},"
            f"volume-subpath={rel.as_posix()}"
        ),
    ]


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
_CACHE_TARGET = "/sbxcache"


def _cache_mount(settings: Settings) -> list[str]:
    """Mount the shared, disk-backed package cache, or ``[]`` when
    disabled or the directory can't be created (best-effort: a missing
    cache must never fail a spawn — it only costs memory).
    """
    if not settings.sandbox_package_cache:
        return []
    cache_dir = Path(settings.data_dir).resolve() / _CACHE_SUBDIR
    try:
        # Created from inside the mill container, which has the data dir
        # mounted; Docker needs the subpath to exist before it can mount it.
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.warning("sandbox package cache unavailable at %s", cache_dir, exc_info=True)
        return []
    if settings.sandbox_data_mount:
        host_src = Path(settings.sandbox_data_mount) / _CACHE_SUBDIR
        return ["-v", f"{host_src}:{_CACHE_TARGET}"]
    return [
        "--mount",
        (
            f"type=volume,src={settings.data_volume},dst={_CACHE_TARGET},"
            f"volume-subpath={_CACHE_SUBDIR}"
        ),
    ]


def _has_uv_sources(repo_dir: Path) -> bool:
    """Return True when pyproject.toml declares dependencies that require
    ``uv sync`` rather than ``pip install``.

    Two signals:

    * ``[tool.uv.sources]`` table (explicit uv source config).
    * PEP 508 ``@ git+https://`` direct references in dependency strings
      (pip cannot resolve git-sourced dependencies declared this way).

    Uses ``tomllib`` (the same pattern as ``prerequisite.py``) and is
    guarded against missing/malformed files — returns ``False`` on any
    error so the sandbox always falls back to the pip install path.
    """
    pp = repo_dir / "pyproject.toml"
    try:
        data = pp.read_text(encoding="utf-8")
    except OSError:
        return False

    # Fast path: PEP 508 git direct references.  A bare string scan
    # for "git+https://" catches every ``@ git+https://`` dependency
    # line without a TOML parse.  False positives (e.g. in comments)
    # are harmless — they just cause uv sync to be used instead of
    # pip install, which is always valid for uv-managed projects.
    if "git+https://" in data:
        return True

    # Check for an explicit [tool.uv.sources] table.
    if "[tool.uv.sources]" not in data:
        return False
    import tomllib

    try:
        parsed = tomllib.loads(data)
    except Exception:
        return False
    sources = parsed.get("tool", {}).get("uv", {}).get("sources")
    return isinstance(sources, dict) and len(sources) > 0


def _maybe_install_prefix(command: str, repo_dir: Path, settings: Settings) -> str:
    """Prepend a read-only-safe project install to *command*, if warranted.

    Returns *command* unchanged unless ALL of:

    * the repo is a Python project (``pyproject.toml`` present), and
    * the sandbox has egress (an egress proxy is configured) — without
      network ``pip`` can't reach PyPI, so installing is impossible and
      we must not turn a runnable gate into a guaranteed failure.

    When the repo declares ``[tool.uv.sources]`` AND a ``uv.lock`` exists,
    the function prefers ``uv sync --frozen --no-dev`` over ``pip install``.
    pip has no equivalent for ``[tool.uv.sources]`` and cannot resolve
    git-sourced dependencies declared there.  ``--frozen`` reads the
    existing lockfile (no git resolution needed) so the sandbox's lack of
    GitHub credentials is NOT a problem.  Falls back to pip when ``uv`` is
    not on ``PATH`` or ``uv sync`` exits non-zero.

    The install is made safe for the locked-down sandbox:

    * ``--user`` installs into ``HOME=/tmp/.local`` — the sandbox's
      writable tmpfs — so it works under the read-only container root.
    * PEP 517 build isolation copies the source to ``TMPDIR=/tmp`` before
      building, so the (writable) repo bind mount is never mutated — no
      stray ``*.egg-info`` written back to the host clone.
    * ``PYTHONPATH=src`` (injected separately for src-layout repos) still
      shadows the freshly-installed package with the MOUNTED edits, so
      the gate tests the ticket's code while importing its declared deps.

    The install is a **best-effort** step: it never blocks the command.
    If the install fails (disk full, network unavailable, missing dep),
    a single warning line is emitted to stderr and the command runs
    regardless with whatever deps the image baked in.  The command's
    own exit code is always preserved — install failure noise no longer
    pollutes every ``run_command`` result or masks ``grep`` exit codes.
    """
    if not settings.sandbox_proxy_url:
        return command
    if not (repo_dir / "pyproject.toml").exists():
        return command

    pip = "pip install --user --quiet --disable-pip-version-check"

    # Stale build artifacts from a prior failed build (e.g. setuptools
    # `[Errno 17] File exists: build/bdist.linux-x86_64/wheel/...`)
    # cause every subsequent pip/uv install to fail until cleaned.
    # Remove them before the install so a single transient build failure
    # doesn't poison the entire sandbox run.
    cleanup = "rm -rf build src/*.egg-info 2>/dev/null; "

    # Disk-space guard: skip the install when /tmp has less than
    # 10 MiB free.  The install writes to /tmp/.local (HOME=/tmp,
    # --user), so a full /tmp tmpfs causes ENOSPC that pollutes
    # every run_command result with multi-line stderr noise.
    disk_guard = (
        "avail=$(df -k /tmp 2>/dev/null | awk 'NR==2{print $4}'); "
        'if [ -n "$avail" ] && [ "$avail" -lt 10240 ]; then '
        'echo "WARNING: sandbox install skipped — /tmp has ${avail} KiB free (need >= 10240)" >&2; '
        "else "
    )

    if _has_uv_sources(repo_dir) and (repo_dir / "uv.lock").exists():
        # UV_MALWARE_CHECK=0 disables uv's OSV malware scan for this
        # install.  The sandbox is network-isolated, so the scan's HTTPS
        # call to api.osv.dev always fails ("tunnel error: unsuccessful")
        # after 3 retries — turning a would-be-successful `--frozen`
        # install (which resolves entirely from the vetted lockfile plus
        # the local cache, fetching nothing new to scan) into a
        # guaranteed failure that floods every run_command result with
        # the OSV error and the "image deps only" banner.  The real
        # supply-chain gate lives in CI (UV_MALWARE_CHECK=1 in the
        # workflows), which is untouched.
        uv = "UV_MALWARE_CHECK=0 uv sync --frozen --no-dev --quiet 2>&1"
        # When the repo declares [tool.uv.sources] AND a uv.lock exists,
        # prefer `uv sync --frozen --no-dev` over pip.  pip has no
        # [tool.uv.sources] equivalent and cannot resolve git-sourced
        # dependencies declared there.  `--frozen` reads the existing
        # lockfile (no git resolution needed) so the sandbox's lack of
        # GitHub credentials is NOT a problem.
        install = (
            f"if command -v uv >/dev/null 2>&1; then ({uv}); else "
            f"echo 'WARNING: uv not found, falling back to pip' >&2; "
            f'({pip} ".[dev]" 2>&1 || {pip} . 2>&1); fi'
        )
    else:
        # No [tool.uv.sources] — pip path.
        # Install the project WITH its dev/test extra so test-only deps
        # the ticket adds (e.g. hypothesis) are importable in the gate —
        # a plain `pip install .` pulls runtime deps only, so a new test
        # dependency fails with ModuleNotFoundError. Try `.[dev]` (the
        # convention across robotsix repos); fall back to a plain install
        # for any repo that has no `dev` extra (pip would otherwise
        # error), so this never regresses a previously-runnable gate.
        install = f'({pip} ".[dev]" 2>&1 || {pip} . 2>&1)'

    # Run install as a best-effort step inside the disk-guard's else
    # branch.  If it fails, emit a single warning line and proceed —
    # the command always runs, and its exit code is always preserved.
    # Previously the install was &&-chained so a failed install
    # silently swallowed the command (and its stderr noise polluted
    # every run_command result, making grep exit codes
    # indistinguishable from install failures).
    return (
        f"{cleanup}"
        f"{disk_guard}"
        f"if {install}; then :; else "
        f'echo "WARNING: sandbox project install failed '
        f'(see above); running command with image deps only" >&2; '
        f"fi; fi; "
        f"{command}"
    )


def _build_extra_packages_prefix(extra_packages: list[str]) -> tuple[str, bool]:
    """Build a shell command prefix that installs extra packages.

    Each entry can be:
    - ``pip:<name>`` → install via ``pip install --user``
    - ``apt:<name>`` → install via ``apt-get install -y``
    - bare ``<name>`` → defaults to apt (the sandbox is Debian-based)

    Returns ``(shell_prefix, needs_write_access)``:
    - *shell_prefix* is the semicolon-chained shell commands ending with
      ``"; "``, or ``""`` when the list is empty.
    - *needs_write_access* is ``True`` when any apt package is present
      (apt must write to the root filesystem, so ``--read-only`` must be
      dropped and tmpfs mounts added for apt state directories).
    """
    if not extra_packages:
        return "", False

    apt_packages: list[str] = []
    pip_packages: list[str] = []

    for pkg in extra_packages:
        if pkg.startswith("pip:"):
            pip_packages.append(pkg[4:])
        elif pkg.startswith("apt:"):
            apt_packages.append(pkg[4:])
        else:
            apt_packages.append(pkg)

    parts: list[str] = []

    if apt_packages:
        parts.append("apt-get update -qq 2>/dev/null || true")
        pkg_list = " ".join(apt_packages)
        parts.append(
            f'for pkg in {pkg_list}; do apt-get install -y -qq "$pkg" '
            f'|| echo "WARNING: failed to install apt package: $pkg"; done'
        )

    if pip_packages:
        for pkg in pip_packages:
            parts.append(
                f"pip install --user --quiet --disable-pip-version-check {pkg} "
                f'|| echo "WARNING: failed to install pip package: {pkg}"'
            )

    if not parts:
        return "", False

    prefix = "; ".join(parts) + "; "
    needs_write = bool(apt_packages)
    return prefix, needs_write


def _parse_docker_started_at(value: str) -> datetime | None:
    """Parse Docker's ``State.StartedAt`` into an aware ``datetime``.

    Docker emits RFC3339 with up to 9 fractional digits and a ``Z`` suffix
    (e.g. ``2026-06-18T20:34:45.483641388Z``).  Returns ``None`` for the
    zero value (a container that never started) or anything unparseable —
    callers treat ``None`` as "leave it alone".
    """
    value = value.strip()
    if not value or value.startswith("0001-01-01"):
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    # ``datetime.fromisoformat`` accepts at most 6 fractional digits;
    # Docker emits 9, so truncate the fractional part to microseconds.
    value = re.sub(r"(\.\d{6})\d+", r"\1", value)
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _list_sandbox_containers() -> list[tuple[str, str]]:
    """Return ``(id, name)`` for every ``mill-sbx-*``/``mill-fetch-*``
    container in ANY state (``docker ps -a``), not just running ones.

    Restarting the mill mid-run kills its in-flight ``docker run`` children,
    leaving their containers stuck in the ``Created`` state (never started).
    Those are invisible to a plain ``docker ps`` (running only), so a
    running-only reaper left them to accumulate and (when the worker blocked
    on the hung ``docker run``) stall the pipeline. Listing all states lets
    the startup reaper (which removes everything, since nothing is legitimately
    running at boot) sweep these leftovers. The age-gated periodic reaper
    still skips ``Created`` containers (they have no StartedAt → treated as
    "leave alone"), so it can't race a sandbox the worker just created.
    Best-effort: an empty list on any Docker CLI failure.
    """
    filters: list[str] = []
    for prefix in _SANDBOX_CONTAINER_PREFIXES:
        filters += ["--filter", f"name={prefix}"]
    try:
        listing = subprocess.run(
            [
                "docker",
                "ps",
                "-a",
                "--no-trunc",
                "--format",
                "{{.ID}}\t{{.Names}}",
                *filters,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError, subprocess.SubprocessError:
        return []
    if listing.returncode != 0:
        return []
    out: list[tuple[str, str]] = []
    for line in listing.stdout.splitlines():
        cid, _, name = line.partition("\t")
        cid = cid.strip()
        if cid:
            out.append((cid, name.strip() or cid))
    return out


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
    import shutil

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


# Re-exports from submodules so `from robotsix_mill.sandbox import run`
# still works unchanged.
from ._fetch import fetch as fetch
from ._lifecycle import run as run
