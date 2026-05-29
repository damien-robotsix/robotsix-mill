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


def try_rebase_onto(
    repo: Path,
    target: str,
    *,
    remote_url: str | None = None,
    token: str | None = None,
) -> bool:
    """Fetch ``<target>`` and rebase the current branch onto it.

    Deterministic (no agent). Returns ``True`` on a clean rebase;
    on any fetch/rebase failure it aborts a half-applied rebase and
    returns ``False`` so the caller can fall back to a fresh clone.
    Used by the implement resume path so a WIP branch pinned to an old
    base picks up current ``main`` (e.g. a fixed test-gate conftest)
    instead of failing the gate forever.

    ``remote_url`` + ``token`` are the GitHub App installation token
    flow used by ``push``/``fetch``: they are token-authed at call time
    via :func:`_authed_url`. Without them the function falls back to
    the clone's stored ``origin`` URL — fine for unauthenticated remotes
    but a footgun for GitHub App tokens (1-hour TTL): a clone made
    hours ago carries an expired token in ``origin``, so ``git fetch
    origin`` returns 401, this function returns False, and the
    implement→rebase loop in implement.py:818 fires every poll because
    the rebase stage's own push/fetch use a freshly minted token and
    never rewrite ``origin``. Passing the fresh token here breaks the
    loop. ff45 hit exactly this on 2026-05-29.

    Any uncommitted edits in the working tree are discarded before the
    rebase. These come exclusively from a server interrupt mid-stage —
    the agent had committed its real progress and started another edit
    when the process was killed. The leftover diff is throwaway state,
    not work-to-preserve; trying to autostash it just carried the
    interrupted edits forward into the next cycle and re-broke things.
    Start from a clean checkout instead.
    """
    # Always-fresh authed URL when the caller has a remote_url;
    # otherwise fall back to the clone's stored origin (no remote_url
    # at all → legacy callers / tests that don't thread auth). When
    # remote_url is set but token is None (e.g. file:// remote in
    # tests), _authed_url passes the URL through unchanged.
    fetch_remote = _authed_url(remote_url, token) if remote_url else "origin"
    try:
        _git(repo, "fetch", fetch_remote, target)
    except subprocess.CalledProcessError:
        return False
    # `git fetch <explicit-url> <target>` writes to FETCH_HEAD but does
    # NOT update `refs/remotes/origin/<target>`, so the subsequent
    # `git rebase origin/<target>` would rebase onto a STALE
    # remote-tracking ref. Update the ref explicitly so the rebase
    # picks up what we just fetched.
    if fetch_remote != "origin":
        try:
            _git(repo, "update-ref", f"refs/remotes/origin/{target}",
                 "FETCH_HEAD")
        except subprocess.CalledProcessError:
            # If the update fails the rebase target will be stale —
            # bail rather than rebase onto an old SHA.
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


def changed_files(repo: Path, target_branch: str) -> list[str]:
    """Return every file that would land in the next commit vs
    ``origin/<target_branch>`` — including untracked new files.

    Union of:
      - ``git diff --name-only origin/<target>`` — tracked-file
        modifications (staged + unstaged).
      - ``git ls-files --others --exclude-standard`` — untracked
        files honouring ``.gitignore``.

    Untracked files matter for scope enforcement: the agent often
    writes new files into the working tree without staging them
    (or runs pytest itself, leaving ``__pycache__/*.pyc`` on disk).
    The next ``commit_all`` runs ``git add -A`` and sweeps them in,
    so scope check must see them BEFORE the commit or it lets the
    out-of-scope additions through.
    """
    seen: list[str] = []
    seen_set: set[str] = set()
    diff_out = _git(repo, "diff", "--name-only", f"origin/{target_branch}")
    if diff_out:
        for f in diff_out.split("\n"):
            if f and f not in seen_set:
                seen_set.add(f)
                seen.append(f)
    untracked_out = _git(repo, "ls-files", "--others", "--exclude-standard")
    if untracked_out:
        for f in untracked_out.split("\n"):
            if f and f not in seen_set:
                seen_set.add(f)
                seen.append(f)
    return seen


def diff_base(
    repo: Path,
    target_branch: str,
    *,
    remote_url: str | None = None,
    token: str | None = None,
) -> str:
    """Return the unified diff of all commits on the current branch
    vs origin/<target_branch>. Fetches first so the diff is current.

    When BOTH *remote_url* and *token* are provided, the fetch goes
    through a fresh token-authed URL — required for private repos
    because the GitHub App installation token baked into ``origin``'s
    URL at clone time expires ~1h later, so a stale clone's later
    fetch would fail with exit 128. Without a token, fall back to
    the clone's existing ``origin`` remote (correct for public
    repos and for tests that set up a local bare repo as origin).
    """
    if remote_url is not None and token is not None:
        _git(
            repo,
            "fetch",
            _authed_url(remote_url, token),
            f"+refs/heads/{target_branch}:refs/remotes/origin/{target_branch}",
        )
    else:
        _git(repo, "fetch", "origin", target_branch)
    return subprocess.run(
        ["git", "-C", str(repo), "diff", f"origin/{target_branch}...HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout
