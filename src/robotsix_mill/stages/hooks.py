"""Per-repo prepare hook.

A managed repo can ship an executable script at
``.robotsix-mill/prepare`` that runs after clone and before the agent
executes in the refine and implement stages. The script receives
context via environment variables (``TICKET_ID``, ``REPO_DIR``,
``WORKSPACE_DIR``) and has 300 s to complete.

This module follows the same hardening contract as
:mod:`robotsix_mill.config.repo_settings`: a managed repo MUST NOT be able to
crash mill by committing a broken script, so every path returns
``None`` (no error) or a short error string rather than raising.
"""

from __future__ import annotations

import logging
import os
import stat
import subprocess
from pathlib import Path

log = logging.getLogger("robotsix_mill.stages.hooks")

PREPARE_SCRIPT = ".robotsix-mill/prepare"
TIMEOUT_SECONDS = 300
MAX_STDERR_CHARS = 500


def run_prepare_hook(
    repo_dir: Path,
    ticket_id: str,
    workspace_dir: Path,
) -> str | None:
    """Run the repo's ``.robotsix-mill/prepare`` script, if present.

    Args:
        repo_dir: Path to the cloned repository root.
        ticket_id: The mill ticket id (exported as ``TICKET_ID``).
        workspace_dir: The ticket workspace directory
            (exported as ``WORKSPACE_DIR``).

    Returns:
        ``None`` when the script is absent or exits 0.
        An error string (≤500 chars of stderr) on non-zero exit or
        timeout.  Never raises — a broken or malicious script must not
        crash mill.
    """
    script_path = repo_dir / PREPARE_SCRIPT
    if not script_path.is_file():
        return None

    # Make executable if needed (best-effort; a truly broken fs entry
    # is caught by the subprocess call below and surfaced as an error).
    try:
        st = script_path.stat()
        if not st.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
            script_path.chmod(st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        log.warning(
            "hooks: could not stat/chmod %s — attempting run anyway",
            script_path,
        )

    env = {
        **os.environ,
        "TICKET_ID": ticket_id,
        "REPO_DIR": str(repo_dir),
        "WORKSPACE_DIR": str(workspace_dir),
    }

    try:
        proc = subprocess.run(  # noqa: S603 — script is repo-controlled, not attacker input
            ["/bin/sh", str(script_path)],
            cwd=str(repo_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        stderr_tail = _truncate_stderr(
            exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        )
        return f"prepare hook timed out after {TIMEOUT_SECONDS}s" + (
            f": {stderr_tail}" if stderr_tail else ""
        )
    except OSError as exc:
        return f"prepare hook could not execute: {exc}"

    if proc.returncode == 0:
        return None

    stderr_tail = _truncate_stderr(proc.stderr or "")
    return f"prepare hook exited {proc.returncode}" + (
        f": {stderr_tail}" if stderr_tail else ""
    )


def _truncate_stderr(stderr: str) -> str:
    """Return at most ``MAX_STDERR_CHARS`` characters of *stderr*."""
    if len(stderr) <= MAX_STDERR_CHARS:
        return stderr
    return stderr[:MAX_STDERR_CHARS] + "…"
