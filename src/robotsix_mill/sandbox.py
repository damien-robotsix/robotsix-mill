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
not inside the mill container. So we mount the *named data volume*
(``MILL_DATA_VOLUME``) at ``MILL_DATA_DIR`` in the sandbox; because mill
also sees the volume at that same path, ``repo_dir`` (an absolute path
under the data dir) lines up on both sides.
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
        "-v", f"{settings.sandbox_data_mount or settings.data_volume}"
              f":{settings.data_dir}",
        "-w", str(repo_dir),
    ]
    if settings.sandbox_readonly:
        argv.append("--read-only")
    argv += [settings.sandbox_image, "sh", "-lc", command]

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
