"""Command execution isolation.

The implement agent's ``run_command`` tool and the stage's test command
run **attacker-influenceable** code (ticket text and cloned repo content
steer the LLM). They must not run in the mill process.

``docker`` mode runs each command in a fresh, disposable sibling
container: ``--network none``, ``--rm``, non-root, read-only root with a
tmpfs ``/tmp``, pids/memory capped, and **only the ticket's repo
reachable** (via the shared data volume — see below).

``local`` mode runs in-process (``shell=True``) with a process-group
kill on timeout. It is *not* isolated and is only for trusted dev/CI
where no Docker socket is available.

Sibling-container mount caveat: when mill talks to the host Docker
daemon over the mounted socket, ``-v`` paths resolve on the **host**,
not inside the mill container. So we mount the *named data volume*
(``MILL_DATA_VOLUME``) at ``MILL_DATA_DIR`` in the sandbox; because mill
also sees the volume at that same path, ``repo_dir`` (an absolute path
under the data dir) lines up on both sides.
"""

from __future__ import annotations

import os
import signal
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


def _run_local(command: str, repo_dir: Path, timeout: int) -> tuple[int, str]:
    # start_new_session => the shell leads its own process group, so on
    # timeout we can SIGKILL the whole tree, not just the shell.
    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=repo_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, _truncate(out or "")
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait()
        return 124, f"command timed out after {timeout}s"


def _run_docker(
    command: str, repo_dir: Path, settings: Settings
) -> tuple[int, str]:
    name = f"mill-sbx-{uuid.uuid4().hex[:12]}"
    argv = [
        "docker", "run", "--rm", "--name", name,
        "--network", "none",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--pids-limit", str(settings.sandbox_pids_limit),
        "--memory", settings.sandbox_memory,
        "--tmpfs", "/tmp",
        "-e", "HOME=/tmp",
        "-v", f"{settings.data_volume}:{settings.data_dir}",
        "-w", str(repo_dir),
    ]
    if settings.sandbox_readonly:
        argv.append("--read-only")
    argv += [settings.sandbox_image, "sh", "-lc", command]

    try:
        r = subprocess.run(
            argv, capture_output=True, text=True, timeout=settings.command_timeout
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
        raise SandboxError(f"docker run failed: {(r.stderr or '').strip()[:300]}")
    return r.returncode, _truncate((r.stdout or "") + (r.stderr or ""))


def run(command: str, *, repo_dir: Path, settings: Settings) -> tuple[int, str]:
    """Execute ``command`` against ``repo_dir``. Returns
    ``(exit_code, combined_output)``. Raises :class:`SandboxError` on
    isolation-infrastructure failure."""
    if settings.sandbox_mode == "local":
        return _run_local(command, repo_dir, settings.command_timeout)
    return _run_docker(command, repo_dir, settings)
