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

import logging
import os
import subprocess
import uuid
from pathlib import Path

from .config import Settings
from .repo_settings import load_extra_sandbox_packages

_OUT_CAP = 8000

log = logging.getLogger("robotsix_mill.sandbox")


class SandboxError(RuntimeError):
    """Infrastructure failure (no Docker, daemon/image error) — distinct
    from the command itself exiting non-zero."""


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


def _maybe_install_prefix(command: str, repo_dir: Path, settings: Settings) -> str:
    """Prepend a read-only-safe project install to *command*, if warranted.

    Returns *command* unchanged unless ALL of:

    * the repo is a Python project (``pyproject.toml`` present), and
    * the sandbox has egress (an egress proxy is configured) — without
      network ``pip`` can't reach PyPI, so installing is impossible and
      we must not turn a runnable gate into a guaranteed failure.

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
    return "pip install --user --quiet --disable-pip-version-check . && " + command


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
        "/tmp",  # nosec B108 — /tmp here is a Docker tmpfs INSIDE the sandbox, not the host's
        "-e",
        "HOME=/tmp",  # nosec B108
        *_repo_mount(repo_dir, settings),
        "-w",
        str(repo_dir),
    ]
    if needs_write_access:
        # apt must write to the root filesystem — drop --read-only and
        # add tmpfs mounts so apt state dirs don't dirty the overlay.
        argv += [
            "--tmpfs", "/var/cache/apt",
            "--tmpfs", "/var/lib/apt/lists",
            "--tmpfs", "/var/lib/dpkg",
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

    # Override the image ENTRYPOINT: images like robotsix/mill have one
    # (it starts the server) which would otherwise swallow our command.
    argv += ["--entrypoint", "sh", settings.sandbox_image, "-lc", effective_command]

    try:
        r = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=settings.command_timeout,
        )
    except FileNotFoundError as e:
        raise SandboxError("docker CLI not found in the mill image") from e
    except subprocess.TimeoutExpired:
        # the `docker run` client was killed; force-remove the container
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)
        return 124, f"command timed out after {settings.command_timeout}s"

    # 125 == docker daemon/usage error (not the command's own exit code)
    if r.returncode == 125:
        raise SandboxError(f"docker run failed: {(r.stderr or '').strip()[:300]}")
    return r.returncode, _truncate((r.stdout or "") + (r.stderr or ""))


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
            text=True,
            timeout=settings.web_fetch_timeout + 15,
        )
    except FileNotFoundError as e:
        raise SandboxError("docker CLI not found in the mill image") from e
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)
        return 124, f"fetch timed out after {settings.web_fetch_timeout}s"

    if r.returncode == 125:
        raise SandboxError(f"docker run failed: {(r.stderr or '').strip()[:300]}")
    body = r.stdout or ""
    if len(body) > settings.web_fetch_max_bytes:
        body = body[: settings.web_fetch_max_bytes] + "\n... [truncated]"
    if r.returncode != 0:
        body = f"(curl exit {r.returncode}) {(r.stderr or '').strip()[:300]}\n{body}"
    return r.returncode, body
