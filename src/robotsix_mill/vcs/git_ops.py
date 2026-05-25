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
    """Return ``True`` if the repo has uncommitted changes."""
    return bool(_git(repo, "status", "--porcelain"))


def branch_exists(repo: Path, name: str) -> bool:
    """Return ``True`` if the local branch *name* exists."""
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
    """Quiet checkout of branch *name*."""
    _git(repo, "checkout", "-q", name)


def try_rebase_onto(repo: Path, target: str) -> bool:
    """Fetch ``origin/<target>`` and rebase the current branch onto it.

    Deterministic (no agent). Returns ``True`` on a clean rebase;
    on any fetch/rebase failure it aborts a half-applied rebase and
    returns ``False`` so the caller can fall back to a fresh clone.
    Used by the implement resume path so a WIP branch pinned to an old
    base picks up current ``main`` (e.g. a fixed test-gate conftest)
    instead of failing the gate forever.

    Any uncommitted edits in the working tree are discarded before the
    rebase. These come exclusively from a server interrupt mid-stage —
    the agent had committed its real progress and started another edit
    when the process was killed. The leftover diff is throwaway state,
    not work-to-preserve; trying to autostash it just carried the
    interrupted edits forward into the next cycle and re-broke things.
    Start from a clean checkout instead.
    """
    try:
        _git(repo, "fetch", "origin", target)
    except subprocess.CalledProcessError:
        return False
    # Discard any leftover uncommitted state from a prior interrupted
    # stage. Best-effort — a failure here just falls through to the
    # rebase, where the original error will surface.
    try:
        _git(repo, "reset", "--hard", "HEAD")
        _git(repo, "clean", "-fd")
    except subprocess.CalledProcessError:
        pass
    try:
        _git(repo, "rebase", f"origin/{target}")
        return True
    except subprocess.CalledProcessError:
        try:
            _git(repo, "rebase", "--abort")
        except subprocess.CalledProcessError:
            pass
        return False


def head_sha(repo: Path) -> str:
    """Current HEAD commit SHA. Used to detect a no-op rebase so the
    merge stage can skip a pointless force-push (an unchanged push still
    re-triggers CI and a GitHub mergeable recompute → state churn)."""
    return _git(repo, "rev-parse", "HEAD")


def remote_branch_sha(repo: Path, branch: str) -> str | None:
    """SHA the remote currently has for *branch* (the rebase agent runs
    ``git fetch origin`` first, so ``origin/<branch>`` is fresh). Returns
    None if the remote has no such branch yet. The merge stage skips the
    force-push only when this equals local HEAD — i.e. the remote truly
    already has this exact commit (not merely a local-rebase no-op)."""
    try:
        return _git(repo, "rev-parse", f"refs/remotes/origin/{branch}")
    except subprocess.CalledProcessError:
        return None


def create_branch(repo: Path, name: str) -> None:
    """Create or reset a branch (``git checkout -B``)."""
    _git(repo, "checkout", "-q", "-B", name)


def commit_all(repo: Path, message: str) -> None:
    """Stage all changes and commit (``git add -A`` + ``git commit -q -m``)."""
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


def fetch(repo: Path, *, remote_url: str, token: str | None, branch: str) -> None:
    """Fetch ``branch`` from ``remote_url`` (token-auth for https) and
    update ``refs/remotes/origin/<branch>``.  Uses an explicit refspec
    so the remote-tracking ref is refreshed even when fetching from
    an explicit URL rather than the clone's origin remote."""
    _git(
        repo,
        "fetch",
        _authed_url(remote_url, token),
        f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
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


def recent_commits(repo: Path, n: int) -> list[dict]:
    """Return the last *n* non-merge commits as ``[{sha, subject}]``.

    Shells out to ``git log --oneline -n <N> --no-merges``.  Gracefully
    handles shallow repos or repos with fewer than *n* commits (``git
    log`` simply returns what it has).  The repo must exist (``.git``
    present); raises ``FileNotFoundError`` otherwise.
    """
    if not (repo / ".git").exists():
        raise FileNotFoundError(f"not a git repository: {repo}")
    output = subprocess.run(
        ["git", "-C", str(repo), "log", "--oneline", "-n", str(n), "--no-merges"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not output:
        return []
    commits = []
    for line in output.split("\n"):
        sha, _, subject = line.partition(" ")
        commits.append({"sha": sha, "subject": subject})
    return commits


def changed_files(repo: Path, target_branch: str) -> list[str]:
    """Return files changed between origin/<target_branch> and the
    working tree (including unstaged modifications to tracked files).

    Runs ``git diff --name-only origin/<target_branch>`` and returns
    the list of changed file paths (relative to repo root).
    An empty diff returns an empty list.
    """
    output = _git(repo, "diff", "--name-only", f"origin/{target_branch}")
    if not output:
        return []
    return output.split("\n")


def diff_base(repo: Path, target_branch: str) -> str:
    """Return the unified diff of all commits on the current branch
    vs the merge-base with origin/<target_branch> (three-dot diff).

    Uses ``A...B`` syntax, which diffs from the common ancestor rather
    than the tip of A.  Changes merged into origin/<target> *after*
    the branch diverged are excluded — only the commits on the current
    branch appear in the diff.

    Fetches first so the remote ref is current (harmless with three-dot
    because the merge-base is determined by commit ancestry)."""
    _git(repo, "fetch", "origin", target_branch)
    return subprocess.run(
        ["git", "-C", str(repo), "diff", f"origin/{target_branch}...HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout
