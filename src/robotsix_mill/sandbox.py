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

import os
import subprocess
import uuid
from pathlib import Path

from .config import Settings

_OUT_CAP = 8000


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
    repo_dir = Path(repo_dir)
    try:
        rel = repo_dir.relative_to(settings.data_dir)
    except ValueError as e:
        raise SandboxError(
            f"repo_dir {repo_dir} is not under data_dir "
            f"{settings.data_dir}; refusing to mount"
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


def run(command: str, *, repo_dir: Path, settings: Settings) -> tuple[int, str]:
    """Execute ``command`` against ``repo_dir`` in a disposable
    container. Returns ``(exit_code, combined_output)``. Raises
    :class:`SandboxError` on isolation-infrastructure failure."""
    name = f"mill-sbx-{uuid.uuid4().hex[:12]}"
    argv = [
        "docker", "run", "--rm", "--name", name,
        "--network", "none",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--pids-limit", str(settings.sandbox_pids_limit),
        "--memory", settings.sandbox_memory,
        "--tmpfs", "/tmp",
        "-e", "HOME=/tmp",
        *_repo_mount(Path(repo_dir), settings),
        "-w", str(repo_dir),
    ]
    if settings.sandbox_readonly:
        argv.append("--read-only")
    # Override the image ENTRYPOINT: images like robotsix/mill have one
    # (it starts the server) which would otherwise swallow our command.
    argv += ["--entrypoint", "sh", settings.sandbox_image, "-lc", command]

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
        subprocess.run(
            ["docker", "rm", "-f", name], capture_output=True, text=True
        )
        return 124, f"command timed out after {settings.command_timeout}s"

    # 125 == docker daemon/usage error (not the command's own exit code)
    if r.returncode == 125:
        raise SandboxError(
            f"docker run failed: {(r.stderr or '').strip()[:300]}"
        )
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
        "docker", "run", "--rm", "--name", name,
        "--read-only", "--tmpfs", "/tmp",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", str(settings.sandbox_pids_limit),
        "--memory", settings.sandbox_memory,
        settings.fetch_image,
        "-sSL",
        "--max-time", str(settings.web_fetch_timeout),
        "--max-filesize", str(settings.web_fetch_max_bytes),
        "-A", "robotsix-mill-fetch",
        "--", url,
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
        subprocess.run(
            ["docker", "rm", "-f", name], capture_output=True, text=True
        )
        return 124, f"fetch timed out after {settings.web_fetch_timeout}s"

    if r.returncode == 125:
        raise SandboxError(
            f"docker run failed: {(r.stderr or '').strip()[:300]}"
        )
    body = r.stdout or ""
    if len(body) > settings.web_fetch_max_bytes:
        body = body[: settings.web_fetch_max_bytes] + "\n... [truncated]"
    if r.returncode != 0:
        body = f"(curl exit {r.returncode}) {(r.stderr or '').strip()[:300]}\n{body}"
    return r.returncode, body
