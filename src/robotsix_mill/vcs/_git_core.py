"""Shared git plumbing — constants and helpers used by both git_ops and git_diff.

Extracted to break the import cycle between git_ops (re-exports git_diff
symbols) and git_diff (needs _git / _authed_url / NETWORK_GIT_TIMEOUT from
git_ops).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


# Wall-clock ceiling for git operations that talk to a remote (clone,
# fetch, push). Without one, a stalled connection hangs the calling
# thread forever — and because every stage offloads its blocking work to
# a shared thread pool, each hung git permanently removes one worker from
# that pool until the process is restarted. Observed live: the whole pool
# wedged in the periodic repo-refresh fetch while merge stages sat
# "active" for 10+ minutes doing nothing. Local-only git calls (log,
# diff, rev-parse) cannot hang on the network and stay unbounded.
NETWORK_GIT_TIMEOUT = 300


def _git_env() -> dict[str, str]:
    """Environment for git subprocesses: never prompt for credentials.

    A git process that decides it needs a username/password will block on
    the terminal indefinitely. There is no terminal here, but git can
    still wait on ``/dev/tty``, so a bad or expired token would hang the
    call rather than failing it. ``GIT_TERMINAL_PROMPT=0`` turns that
    into a clean non-zero exit.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(repo: Path, *args: str, timeout: float | None = None) -> str:
    """Run a git command in *repo* and return its stripped stdout.

    Raises :class:`subprocess.CalledProcessError` on non-zero exit.
    """
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_git_env(),
    ).stdout.strip()


def _authed_url(url: str, token: str | None) -> str:
    """Inject a token into an https remote for non-interactive clone/push.

    Other schemes (file://, ssh) are returned unchanged. Never log the
    result — it contains the credential.
    """
    if token and url.startswith("https://"):
        return url.replace("https://", f"https://oauth2:{token}@", 1)
    return url
