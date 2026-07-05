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
import re
import socket
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..config import Settings
from ..config.repo_settings import load_extra_sandbox_packages

_OUT_CAP = 8000

# Name prefixes of the disposable sibling containers this module spawns:
# ``run()`` uses ``mill-sbx-*`` and ``fetch()`` uses ``mill-fetch-*``.
_SANDBOX_CONTAINER_PREFIXES = ("mill-sbx-", "mill-fetch-")

log = logging.getLogger("robotsix_mill.sandbox")


class SandboxError(RuntimeError):
    """Infrastructure failure (no Docker, daemon/image error) — distinct
    from the command itself exiting non-zero."""


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
    ``-w`` and any absolute path in the repo still resolve."""
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
        f"type=volume,src={settings.data_volume},dst={target},"
        f"volume-subpath={rel.as_posix()}",
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

    Build/runtime deps that are already baked into the image are simply
    re-resolved as already-satisfied — cheap. The win is the deps the
    image lacks (the ticket's newly-added ones)."""
    if not settings.sandbox_proxy_url:
        return command
    if not (repo_dir / "pyproject.toml").exists():
        return command

    pip = "pip install --user --quiet --disable-pip-version-check"

    # When the repo declares [tool.uv.sources] AND a uv.lock exists, prefer
    # `uv sync --frozen --no-dev` over pip.  pip has no [tool.uv.sources]
    # equivalent and cannot resolve git-sourced dependencies declared there.
    # `--frozen` reads the existing lockfile (no git resolution needed) so
    # the sandbox's lack of GitHub credentials is NOT a problem.
    if _has_uv_sources(repo_dir) and (repo_dir / "uv.lock").exists():
        uv = "uv sync --frozen --no-dev --quiet 2>&1"
        return (
            f"(command -v uv >/dev/null 2>&1 && ({uv}) || "
            f"(echo 'WARNING: uv not found, falling back to pip' >&2; "
            f"({pip} '.[dev]' || {pip} .))) && " + command
        )

    # No [tool.uv.sources] — pip path unchanged.
    # Install the project WITH its dev/test extra so test-only deps the
    # ticket adds (e.g. hypothesis) are importable in the gate — a plain
    # `pip install .` pulls runtime deps only, so a new test dependency
    # fails with ModuleNotFoundError. Try `.[dev]` (the convention across
    # robotsix repos); fall back to a plain install for any repo that has
    # no `dev` extra (pip would otherwise error), so this never regresses
    # a previously-runnable gate.
    return f"({pip} '.[dev]' || {pip} .) && " + command


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


def run(  # noqa: C901 — extra-packages loading adds one branch; tightly-coupled argv construction
    command: str,
    *,
    repo_dir: Path,
    settings: Settings,
    install_project: bool = False,
    sandbox_image: str | None = None,
) -> tuple[int, str]:
    """Execute ``command`` against ``repo_dir`` in a disposable
    container. Returns ``(exit_code, combined_output)``. Raises
    :class:`SandboxError` on isolation-infrastructure failure.

    When *install_project* is set (the test gate passes it), the repo's
    own dependencies are installed before *command* runs. The gate
    otherwise executes against the sandbox image's FROZEN site-packages,
    so a ticket that adds a new third-party runtime dependency (e.g.
    converting to Jinja2 templates → adds ``jinja2``) fails forever with
    ``ModuleNotFoundError`` no matter how the agent edits the code —
    because nothing ever installs the declared dependency. See
    ``_maybe_install_prefix`` for how the install is made
    read-only-safe."""
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
        "/tmp:exec,rw,nosuid,nodev",  # nosec B108 — /tmp here is a Docker tmpfs INSIDE the sandbox, not the host's
        "-e",
        "HOME=/tmp",  # nosec B108
        "-e",
        "GIT_TERMINAL_PROMPT=0",
        *_repo_mount(repo_dir, settings),
        "-w",
        str(repo_dir),
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

    try:
        r = subprocess.run(
            argv,
            capture_output=True,
            text=False,
            timeout=settings.command_timeout,
        )
    except FileNotFoundError as e:
        raise SandboxError("docker CLI not found in the mill image") from e
    except subprocess.TimeoutExpired:
        # the `docker run` client was killed; force-remove the container
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=False)
        return 124, f"command timed out after {settings.command_timeout}s"

    stdout = r.stdout.decode("utf-8", errors="replace") if r.stdout else ""
    stderr = r.stderr.decode("utf-8", errors="replace") if r.stderr else ""
    # 125 == docker daemon/usage error (not the command's own exit code)
    if r.returncode == 125:
        raise SandboxError(f"docker run failed: {stderr.strip()[:300]}")
    return r.returncode, _truncate(stdout + stderr)


def fetch(url: str, *, settings: Settings) -> tuple[int, str]:
    """HTTP(S) GET ``url`` in a dedicated, network-ENABLED container.

    Deliberately weaker isolation than :func:`run` (network is on), so
    it is locked down the other way: NO repo/data mount (nothing local
    to exfiltrate), non-root, read-only, caps dropped, no-new-privs,
    fixed ``curl`` (not a shell — the URL is a plain argv item, no
    injection), size/time capped. Residual risk: an agent can encode
    data into the URL it asks to fetch. http(s) only."""
    if not (url.startswith("http://") or url.startswith("https://")):
        return 1, f"refused: only http(s) URLs allowed: {url!r}"

    name = f"mill-fetch-{uuid.uuid4().hex[:12]}"
    argv = [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--read-only",
        "--tmpfs",
        "/tmp",  # nosec B108 — Docker tmpfs INSIDE the sandbox container, not host /tmp
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(settings.sandbox_pids_limit),
        "--memory",
        settings.sandbox_memory,
        settings.fetch_image,
        "-sSL",
        "--max-time",
        str(settings.web_fetch_timeout),
        "--max-filesize",
        str(settings.web_fetch_max_bytes),
        "-A",
        "robotsix-mill-fetch",
        "--",
        url,
    ]
    try:
        r = subprocess.run(
            argv,
            capture_output=True,
            text=False,  # was text=True — avoid UnicodeDecodeError
            timeout=settings.web_fetch_timeout + 15,
        )
    except FileNotFoundError as e:
        raise SandboxError("docker CLI not found in the mill image") from e
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)
        return 124, f"fetch timed out after {settings.web_fetch_timeout}s"

    # Decode stdout/stderr with replacement for non-UTF-8 bytes
    stderr = r.stderr.decode("utf-8", errors="replace") if r.stderr else ""
    body = r.stdout.decode("utf-8", errors="replace") if r.stdout else ""

    if r.returncode == 125:
        raise SandboxError(f"docker run failed: {stderr.strip()[:300]}")
    if len(body) > settings.web_fetch_max_bytes:
        body = body[: settings.web_fetch_max_bytes] + "\n... [truncated]"
    if r.returncode != 0:
        body = f"(curl exit {r.returncode}) {stderr.strip()[:300]}\n{body}"
    return r.returncode, body


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
    Best-effort: an empty list on any Docker CLI failure."""
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
    return (datetime.now(timezone.utc) - started).total_seconds() > max_age_seconds


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
