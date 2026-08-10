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
import os
import socket
import subprocess
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..config import Settings
from ..config.repo_settings import load_extra_sandbox_packages
from ._fetch import (
    SandboxError as SandboxError,
)
from ._fetch import (
    _list_sandbox_containers as _list_sandbox_containers,
)
from ._fetch import (
    _parse_docker_started_at as _parse_docker_started_at,
)
from ._fetch import (
    fetch as fetch,
)
from ._lifecycle import (
    _CACHE_SUBDIR as _CACHE_SUBDIR,
)
from ._lifecycle import (
    live_sandbox_workspace_paths as live_sandbox_workspace_paths,
)
from ._lifecycle import (
    prune_package_cache as prune_package_cache,
)
from ._lifecycle import (
    reap_orphan_sandboxes as reap_orphan_sandboxes,
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

log = logging.getLogger("robotsix_mill.sandbox")


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
        uv = "uv sync --frozen --no-dev --quiet 2>&1"
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


def run(
    command: str,
    *,
    repo_dir: Path,
    settings: Settings,
    install_project: bool = True,
    sandbox_image: str | None = None,
) -> tuple[int, str]:
    """Execute ``command`` against ``repo_dir`` in a disposable
    container. Returns ``(exit_code, combined_output)``. Raises
    :class:`SandboxError` on isolation-infrastructure failure.

    By default the repo's own dependencies are installed before
    *command* runs so that the workspace clone — not the image's frozen
    site-packages — is the imported tree.  Callers that must skip the
    install (e.g. ad-hoc commands in an environment with no egress
    proxy) can pass *install_project=False*.  See
    ``_maybe_install_prefix`` for how the install is made
    read-only-safe.
    """
    # Callers (e.g. the merge stage) may pass a str. We also resolve to
    # an absolute path because Docker's `-w` rejects relative arguments
    # (see _repo_mount for the same reason).
    repo_dir = Path(repo_dir).resolve()
    # Deploy mode only (DOCKER_HOST points at central-deploy's socket-proxy):
    # central-deploy ignores the dev stack's `networks:` block, so the
    # internal egress network + proxy attachment must be established at
    # runtime. Runs before EVERY spawn (idempotent, two fast docker CLI
    # calls): a deploy can recreate the sandbox-proxy sibling at any time,
    # detaching it from the network — a once-per-process guard left all
    # subsequent sandboxes without egress until the mill itself restarted
    # (2026-07-05 incident: every test suite failed with pytest missing).
    # The dev stack (no DOCKER_HOST) skips it entirely and is unchanged.
    if os.environ.get("DOCKER_HOST") and settings.sandbox_proxy_url:
        ensure_sandbox_network(settings)
    # Load extra sandbox packages declared in the repo's config.
    extra_packages = load_extra_sandbox_packages(repo_dir)
    extra_prefix, needs_write_access = _build_extra_packages_prefix(extra_packages)
    if extra_prefix:
        log.info("Installing extra sandbox packages: %s", extra_packages)
        if needs_write_access:
            log.info(
                "Dropping --read-only and adding tmpfs mounts for apt "
                "package installation"
            )
    name = f"mill-sbx-{uuid.uuid4().hex[:12]}"
    argv = [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--network",
        settings.sandbox_network if settings.sandbox_proxy_url else "none",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--pids-limit",
        str(settings.sandbox_pids_limit),
        "--memory",
        settings.sandbox_memory,
        "--tmpfs",
        # Mount exec (Docker's default tmpfs options include noexec): pip
        # --user console scripts land under $HOME/.local/bin = /tmp/.local/bin
        # and must be executable. Keep nosuid/nodev hardening.
        #
        # size= matters: an unsized Docker tmpfs defaults to half of HOST RAM,
        # so /tmp could grow until it hit the container's memory cgroup and the
        # kernel OOM-killed the test process — a confusing "Killed" with no
        # explanation. Sized, an overflowing sandbox fails loudly with ENOSPC.
        f"/tmp:exec,rw,nosuid,nodev,size={settings.sandbox_tmpfs_size}",  # nosec B108 — /tmp here is a Docker tmpfs INSIDE the sandbox, not the host's
        "-e",
        "HOME=/tmp",  # nosec B108
        "-e",
        "GIT_TERMINAL_PROMPT=0",
        *_repo_mount(repo_dir, settings),
        "-w",
        str(repo_dir),
    ]
    # Bound CPU the way memory and PIDs already are. Without this the
    # concurrency cap bounds the sandbox COUNT while host load stays
    # unbounded — N sandboxes each take whatever their test command's
    # parallelism allows, which is what kept the safe cap so low.
    if settings.sandbox_cpus > 0:
        # Docker wants a plain decimal; repr() on a float can emit
        # scientific notation for very small values, which it rejects.
        argv += ["--cpus", f"{settings.sandbox_cpus:.3f}".rstrip("0").rstrip(".")]
    # Keep the package caches OUT of the RAM-backed /tmp (see _cache_mount).
    # XDG_CACHE_HOME covers uv's default location and most other tools;
    # UV_CACHE_DIR/PIP_CACHE_DIR are set explicitly so neither depends on it.
    cache_argv = _cache_mount(settings)
    if cache_argv:
        argv += cache_argv
        argv += [
            "-e",
            f"XDG_CACHE_HOME={_CACHE_TARGET}",
            "-e",
            f"UV_CACHE_DIR={_CACHE_TARGET}/uv",
            "-e",
            f"PIP_CACHE_DIR={_CACHE_TARGET}/pip",
        ]
    if needs_write_access:
        # apt must write to the root filesystem — drop --read-only and
        # add tmpfs mounts so apt state dirs don't dirty the overlay.
        argv += [
            "--tmpfs",
            "/var/cache/apt",
            "--tmpfs",
            "/var/lib/apt/lists",
            "--tmpfs",
            "/var/lib/dpkg",
        ]
    elif settings.sandbox_readonly:
        argv.append("--read-only")
    # When the mounted repo has a src/ layout, put its source first on
    # PYTHONPATH so the command runs against the MOUNTED code — not a
    # stale copy of the package baked into the sandbox image's
    # site-packages. Without this the in-sandbox test gate silently
    # validates the image's old code instead of the ticket's edits.
    if (repo_dir / "src").is_dir():
        argv += ["-e", "PYTHONPATH=src"]
    # Route HTTP/HTTPS through the egress proxy so only allowlisted
    # domains (PyPI, GitHub) are reachable from the sandbox.
    if settings.sandbox_proxy_url:
        proxy = settings.sandbox_proxy_url
        # Loopback must bypass the proxy: a repo's own test suite often spins
        # up a localhost HTTP server and connects to it (e.g. auto-mail's
        # tests/test_server.py). Without no_proxy those connections get routed
        # to the egress proxy and fail (the proxy filters non-allowlisted
        # hosts, and the suite's network guard flags the real connection).
        no_proxy = "localhost,127.0.0.1,::1"
        argv += [
            "-e",
            f"HTTP_PROXY={proxy}",
            "-e",
            f"HTTPS_PROXY={proxy}",
            "-e",
            f"http_proxy={proxy}",
            "-e",
            f"https_proxy={proxy}",
            "-e",
            f"NO_PROXY={no_proxy}",
            "-e",
            f"no_proxy={no_proxy}",
        ]
    # Optionally prefix a dependency install so the gate runs against the
    # repo's DECLARED deps, not just the image's frozen ones.
    # Extra packages are installed FIRST so they are available when the
    # project build runs (and when the user command executes).
    effective_command = extra_prefix + command
    if install_project:
        effective_command = _maybe_install_prefix(effective_command, repo_dir, settings)

    # Put the pip ``--user`` scripts dir on PATH so console-script entry
    # points (e.g. yamllint installed via extra_sandbox_packages) resolve.
    # pip installs them under ``$HOME/.local/bin`` = ``/tmp/.local/bin``
    # (HOME is fixed to /tmp above) — a dir NOT on the image's PATH, so
    # without this a gate calling such a script dies with rc=127. The
    # export must live inside the ``sh -lc`` string (docker ``-e`` does
    # no shell expansion) and be the FIRST statement so it is in effect
    # for the extra-package install, the project install, and the user
    # command alike.
    effective_command = (
        'export PATH="$HOME/.local/bin:/tmp/.local/bin:$PATH"; ' + effective_command
    )

    # Override the image ENTRYPOINT: images like robotsix/mill have one
    # (it starts the server) which would otherwise swallow our command.
    # Per-repo override (sandbox_image) wins; None falls back to the
    # fleet-wide settings.sandbox_image so existing callers are unchanged.
    image = sandbox_image or settings.sandbox_image
    argv += ["--entrypoint", "sh", image, "-lc", effective_command]

    # Per-op timeout: use the dedicated sandbox_op_timeout when set,
    # falling back to the legacy command_timeout (1800s).  A single
    # hung docker exec must fast-fail in ~5 min (default 300s) instead
    # of silently draining the stage budget for 30 min.
    op_timeout = (
        settings.sandbox_op_timeout
        if settings.sandbox_op_timeout > 0
        else settings.command_timeout
    )
    max_attempts = 3
    # The slot is held across the retries on purpose: a retry re-spawns the
    # same container name, so releasing between attempts would let the live
    # count exceed the cap. It is taken as late as possible — argv building
    # and the extra-package probe above touch no containers.
    with _sandbox_slot(settings):
        for attempt in range(1, max_attempts + 1):
            try:
                r = subprocess.run(
                    argv,
                    capture_output=True,
                    text=False,
                    timeout=op_timeout,
                )
            except FileNotFoundError as e:
                raise SandboxError("docker CLI not found in the mill image") from e
            except subprocess.TimeoutExpired:
                # the `docker run` client was killed; force-remove the container
                subprocess.run(
                    ["docker", "rm", "-f", name], capture_output=True, text=False
                )
                return 124, f"command timed out after {op_timeout}s"

            stdout = r.stdout.decode("utf-8", errors="replace") if r.stdout else ""
            stderr = r.stderr.decode("utf-8", errors="replace") if r.stderr else ""
            # 125 == docker daemon/usage error (not the command's own exit code)
            if r.returncode == 125:
                eof_msg = stderr.strip()
                if "unexpected EOF" in eof_msg and attempt < max_attempts:
                    log.warning(
                        "sandbox spawn attempt %d/%d: docker wait stream EOF "
                        "(socket-proxy timeout likely); retrying...",
                        attempt,
                        max_attempts,
                    )
                    # Clean up any container that may have leaked
                    subprocess.run(
                        ["docker", "rm", "-f", name],
                        capture_output=True,
                        text=False,
                    )
                    continue
                raise SandboxError(f"docker run failed: {eof_msg[:300]}")
            return r.returncode, _truncate(stdout + stderr)
    # Should not be reachable — the last attempt either returns or raises
    # above (non-125 → return; 125 non-EOF → raise; 125 EOF on last
    # attempt → raise).  Included as a safety net.
    raise SandboxError("docker run failed: unexpected EOF after all retries")
