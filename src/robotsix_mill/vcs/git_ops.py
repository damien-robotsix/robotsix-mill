"""Thin git helpers over a per-ticket clone living in its workspace.

The implement stage clones the target repo fresh per ticket; the deliver
stage pushes the branch later. These wrappers shell out to ``git`` so
the container only needs the git binary (already in the image).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_CREDENTIAL_IN_URL = re.compile(r"://[^@/\s']+@")


def redact_credentials(text: str) -> str:
    """Strip ``user:token@`` userinfo from any URL embedded in *text*.

    Error paths that repr a failed git command (``CalledProcessError``
    includes the full argv) would otherwise echo the tokenized remote —
    ``https://oauth2:ghs_…@github.com/…`` — into ticket notes and
    Langfuse traces. Run every git-command error string through this
    before it leaves the process."""
    return _CREDENTIAL_IN_URL.sub("://***@", text)


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


def clone(remote_url: str, dest: Path, branch: str, token: str | None = None) -> None:
    """Single-branch clone of ``branch`` into ``dest`` (fresh per ticket).

    On failure raises :class:`subprocess.CalledProcessError` with the
    tokenized URL redacted from ``cmd`` and ``stderr`` — the repr of this
    error routinely ends up in ticket notes and traces."""
    try:
        subprocess.run(
            [
                "git",
                "clone",
                "--quiet",
                "--single-branch",
                "--branch",
                branch,
                _authed_url(remote_url, token),
                str(dest),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise subprocess.CalledProcessError(
            exc.returncode,
            [redact_credentials(str(a)) for a in exc.cmd],
            output=redact_credentials(exc.output or ""),
            stderr=redact_credentials(exc.stderr or ""),
        ) from None
    _git(dest, "config", "user.email", "mill@robotsix.local")
    _git(dest, "config", "user.name", "robotsix-mill")


def init_repo(dest: Path, branch: str) -> None:
    """Initialise a fresh, empty git repo at ``dest`` with ``branch`` as the
    initial branch and the mill's commit identity configured.

    Use this to scaffold a brand-new (empty) remote: a freshly-created GitHub
    repo (``auto_init: false``) has no branches, so ``clone --branch <main>``
    fails. ``init`` + force-``push`` populates the default branch instead.
    """
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--quiet", "-b", branch, str(dest)],
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
            [
                "git",
                "-C",
                str(repo),
                "rev-parse",
                "--verify",
                "--quiet",
                f"refs/heads/{name}",
            ],
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
            _git(repo, "update-ref", f"refs/remotes/origin/{target}", "FETCH_HEAD")
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


def branch_is_ahead_of_main(repo: Path, target_branch: str = "main") -> bool:
    """Return True when HEAD has commits not in origin/main.

    Counts commits on HEAD that are NOT on ``origin/main`` (the
    ``rev-list origin/main..HEAD`` semantic). A branch that is
    *behind* main but not ahead — typical when the workspace clone's
    local refs are stale and the branch was never updated with new
    commits — returns False, which routes the empty branch to DONE
    in the deliver stage instead of producing a GitHub 422 "No
    commits between main and branch".

    A content-diff check (the previous implementation) doesn't
    distinguish ahead from behind: a stale branch that is BEHIND
    main shows a non-empty diff (all the work landed on main after
    the branch was created) and the old code reported "ahead",
    pushing the branch to the forge which then rejected it.

    Fetches ``origin main`` first so the local ref is current. A
    fetch failure is treated as "ahead" so delivery proceeds — we
    would rather hit the forge API than block a real change.
    """
    try:
        _git(repo, "fetch", "origin", target_branch)
    except subprocess.CalledProcessError:
        # fetch failed — assume ahead so we don't block a real change
        return True

    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "rev-list",
            "--count",
            f"origin/{target_branch}..HEAD",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # rev-list failed — assume ahead so we don't block a real change
        return True
    try:
        return int(result.stdout.strip()) > 0
    except ValueError:
        return True


def branch_has_net_diff(repo: Path, target_branch: str = "main") -> bool:
    """Return True when HEAD has a non-empty content diff vs ``origin/main``.

    Uses the three-dot ``git diff --quiet origin/main...HEAD`` semantic
    (compare HEAD against the merge-base), which is exactly what the forge
    evaluates when opening a PR. This is distinct from
    :func:`branch_is_ahead_of_main`, which counts *commits*: a branch can carry
    a commit that is not on main by SHA (ahead by commit count) yet whose net
    content is identical to main — e.g. main independently landed the same
    change, or the commit was a no-op. The forge rejects such a PR with a 422
    "No commits between main and branch", so deliver must check the net diff,
    not just the commit count, before opening one.

    Fetches ``origin main`` first so the local ref is current. A fetch or diff
    failure returns True (assume there IS a diff) so delivery proceeds — we
    would rather hit the forge API than silently DONE a real change.
    """
    try:
        _git(repo, "fetch", "origin", target_branch)
    except subprocess.CalledProcessError:
        return True

    # `git diff --quiet` exits 0 when there is NO diff, 1 when there is one.
    result = subprocess.run(
        ["git", "-C", str(repo), "diff", "--quiet", f"origin/{target_branch}...HEAD"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return False
    if result.returncode == 1:
        return True
    # Any other exit code is an error (bad ref, etc.) — assume a diff so we
    # don't wrongly route a real change to DONE.
    return True


def branch_is_behind_main(repo: Path, target_branch: str = "main") -> bool:
    """Return True when ``origin/main`` has commits not on HEAD.

    Counts commits on ``origin/main`` that are NOT on HEAD (the
    ``rev-list HEAD..origin/main`` semantic) — i.e. HEAD was cut from an older
    main and main has advanced since. The merge stage uses this to rebase a
    stale PR branch onto current main BEFORE handing a CI failure to ci_fix: a
    repo-wide gate (ruff/mypy) often fails on code that isn't the ticket's
    because main gained a fix the branch lacks; a rebase fixes it, ci_fix can't.

    Fetches ``origin main`` first so the local ref is current. A fetch or
    rev-list failure returns False — don't trigger a pointless rebase on a
    transient git error; the genuine-failure path (ci_fix) runs instead.
    """
    try:
        _git(repo, "fetch", "origin", target_branch)
    except subprocess.CalledProcessError:
        return False
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "rev-list",
            "--count",
            f"HEAD..origin/{target_branch}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    try:
        return int(result.stdout.strip()) > 0
    except ValueError:
        return False


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


def introduced_files(repo: Path, target_branch: str) -> list[str]:
    """Return every file the BRANCH introduces relative to its merge base
    with ``origin/<target_branch>`` — i.e. what the ticket itself changed,
    NOT files that ``origin/<target>`` modified after the branch was cut.

    Union of (order-preserving, deduplicated):
      - ``git diff --name-only origin/<target>...HEAD`` (THREE-dot) —
        committed branch changes vs the merge base. Three-dot diffs
        against the merge base, so files changed on <target> after the
        branch base do NOT appear.
      - ``git diff --name-only HEAD`` — uncommitted tracked working-tree
        changes (staged + unstaged) not yet in HEAD.
      - ``git ls-files --others --exclude-standard`` — untracked new
        files honouring .gitignore.
    """
    seen: list[str] = []
    seen_set: set[str] = set()
    committed_out = _git(repo, "diff", "--name-only", f"origin/{target_branch}...HEAD")
    if committed_out:
        for f in committed_out.split("\n"):
            if f and f not in seen_set:
                seen_set.add(f)
                seen.append(f)
    working_out = _git(repo, "diff", "--name-only", "HEAD")
    if working_out:
        for f in working_out.split("\n"):
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


def restore_paths(repo: Path, target_branch: str, paths: list[str]) -> None:
    """Drop *paths* from the branch's effective diff vs ``origin/<target>``.

    Used to undo scope-triage-REJECTed out-of-scope changes before the
    next iteration. For each path:

    - If it exists in ``origin/<target_branch>``, restore that version
      (``git checkout origin/<target> -- path``) — reverting any tracked
      modification, whether unstaged or already WIP-committed.
    - Otherwise it is a new file: drop it from the index if tracked
      (``git rm``, covering WIP-committed additions) and delete it from
      disk if it still exists (covering untracked additions).

    After this, :func:`changed_files` no longer reports *paths*, and a
    subsequent :func:`commit_all` records the cleaned tree — so the
    rejected paths are absent from the diff vs origin in both the
    unstaged and the WIP-committed cases.
    """
    ref = f"origin/{target_branch}"
    for p in paths:
        rel = p.lstrip("/")
        if not rel:
            continue
        in_origin = (
            subprocess.run(
                ["git", "-C", str(repo), "cat-file", "-e", f"{ref}:{rel}"],
                capture_output=True,
            ).returncode
            == 0
        )
        if in_origin:
            subprocess.run(
                ["git", "-C", str(repo), "checkout", ref, "--", rel],
                capture_output=True,
                text=True,
            )
            continue
        # Not in origin — a newly added file. Drop a tracked
        # (incl. WIP-committed) version from the index, then remove
        # any leftover untracked file from disk.
        subprocess.run(
            ["git", "-C", str(repo), "rm", "-f", "--ignore-unmatch", "--", rel],
            capture_output=True,
            text=True,
        )
        file_path = repo / rel
        try:
            if file_path.exists():
                file_path.unlink()
        except OSError:
            pass


def ignored_existing_paths(repo: Path, paths: list[str]) -> list[str]:
    """Of *paths* (repo-relative), return those that exist on disk but are
    gitignored — i.e. invisible to ``status``/``diff``/``ls-files``.

    This is the "edits landed but git can't see them" detector: a manifest
    board (e.g. a ROS 2 workspace repo whose ``.gitignore`` carries
    ``/src/*`` for vcs-imported sub-repos) lets an agent write real files
    that never reach a diff, which otherwise surfaces only as an opaque
    "no changes produced" block."""
    hits: list[str] = []
    for p in paths:
        rel = p.lstrip("/")
        if not rel or not (repo / rel).exists():
            continue
        rc = subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "--quiet", "--", rel],
            capture_output=True,
        ).returncode
        if rc == 0:
            hits.append(rel)
    return hits


def ignored_paths(repo: Path, paths: list[str]) -> list[str]:
    """Subset of *paths* that are gitignored in *repo*, whether or not
    they currently exist on disk (unlike :func:`ignored_existing_paths`).

    ``git check-ignore --quiet`` matches against ignore rules regardless
    of on-disk existence — including nested paths under an ignored
    directory (e.g. ``src/ros2/foo/Status.msg`` against a ``/src/*``
    rule). Used by the refine guard to reject specs whose ``file_map``
    targets paths the board cannot deliver (vcs-imported / vendored
    sub-trees managed via ``repos.yaml``, invisible to git)."""
    hits: list[str] = []
    for p in paths:
        rel = p.lstrip("/")
        if not rel:
            continue
        rc = subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "--quiet", "--", rel],
            capture_output=True,
        ).returncode
        if rc == 0:
            hits.append(rel)
    return hits


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
        check=True,
        capture_output=True,
        text=True,
    ).stdout
