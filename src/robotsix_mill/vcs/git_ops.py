"""Thin git helpers over a per-ticket clone living in its workspace.

The implement stage clones the target repo fresh per ticket; the deliver
stage pushes the branch later. These wrappers shell out to ``git`` so
the container only needs the git binary (already in the image).
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _authed_url(url: str, token: str | None) -> str:
    """Inject a token into an https remote for non-interactive clone/push.
    Other schemes (file://, ssh) are returned unchanged. Never log the
    result — it contains the credential."""
    if token and url.startswith("https://"):
        return url.replace("https://", f"https://oauth2:{token}@", 1)
    return url


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def ensure_repo(repo: Path) -> None:
    """Init the work repo if it doesn't exist yet."""
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "mill@robotsix.local")
        _git(repo, "config", "user.name", "robotsix-mill")


def clone(
    remote_url: str, dest: Path, branch: str, token: str | None = None
) -> None:
    """Single-branch clone of ``branch`` into ``dest`` (fresh per ticket)."""
    subprocess.run(
        [
            "git", "clone", "--quiet", "--single-branch",
            "--branch", branch, _authed_url(remote_url, token), str(dest),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(dest, "config", "user.email", "mill@robotsix.local")
    _git(dest, "config", "user.name", "robotsix-mill")


def has_changes(repo: Path) -> bool:
    return bool(_git(repo, "status", "--porcelain"))


def branch_exists(repo: Path, name: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
             f"refs/heads/{name}"],
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )


def checkout(repo: Path, name: str) -> None:
    _git(repo, "checkout", "-q", name)


def current_branch(repo: Path) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD")


def create_branch(repo: Path, name: str) -> None:
    _git(repo, "checkout", "-q", "-B", name)


def commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def push(repo: Path, branch: str, remote_url: str, token: str | None) -> None:
    """Push ``branch`` to ``remote_url`` (token-auth for https). Uses
    ``--force`` so a re-delivery updates the bot-owned branch; pushes to
    the explicit authed URL rather than the clone's origin (the clone
    may have been made without a write token, and there is no
    remote-tracking ref to lease against on an explicit-URL push)."""
    _git(
        repo,
        "push",
        "--force",
        _authed_url(remote_url, token),
        f"{branch}:{branch}",
    )


def branch_is_ahead_of_main(repo: Path) -> bool:
    """Return True when HEAD has commits not in origin/main.

    Fetches ``origin main`` first to avoid a stale local ref causing a
    false negative.  A fetch or diff failure other than "no diff" (exit
    1) is treated as "ahead" so delivery proceeds — we would rather hit
    the forge API than block a real change.
    """
    try:
        _git(repo, "fetch", "origin", "main")
    except subprocess.CalledProcessError:
        # fetch failed — assume ahead so we don't block a real change
        return True

    result = subprocess.run(
        ["git", "-C", str(repo), "diff", "--quiet", "origin/main..HEAD"],
        capture_output=True,
        text=True,
    )
    # --quiet exits 0 when there is *no* difference, 1 when there is.
    if result.returncode == 0:
        return False  # no diff → not ahead
    # exit 1 = diff exists (ahead); anything else is an error → assume ahead
    return True
