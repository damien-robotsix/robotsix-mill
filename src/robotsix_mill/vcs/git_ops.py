"""Thin git helpers over a per-ticket clone living in its workspace.

The implement stage clones the target repo fresh per ticket; the deliver
stage pushes the branch later. These wrappers shell out to ``git`` so
the container only needs the git binary (already in the image).
"""

from __future__ import annotations

import logging
import re
import subprocess
from enum import Enum
from pathlib import Path

log = logging.getLogger("robotsix_mill.vcs.git_ops")

_CREDENTIAL_IN_URL = re.compile(r"://[^@/\s']+@")


class ReconcileResult(str, Enum):
    """Outcome of :func:`reconcile_with_remote_pr`.

    ``SYNCED`` — the workspace already matches the remote PR branch, was
        fast-forwarded onto it, is strictly ahead of it, or the remote
        branch doesn't exist yet (first push). Safe to proceed.
    ``DIVERGED`` — the workspace and the remote PR branch have BOTH
        advanced independently AND at least one commit the remote carries
        (that a force-push would discard) is FOREIGN — i.e. authored by
        someone other than the mill (a human pushed to the PR after the
        clone). A force-push here would silently overwrite that foreign
        commit — ``push_with_lease`` does NOT protect this case, because
        reconcile's own fetch already advanced the lease ref to it, so the
        compare-and-swap would pass. Callers MUST block instead of pushing.
        NOTE: divergence where every discarded remote commit is
        mill-authored (the mill's OWN prior force-push from an earlier
        rebase cycle) returns ``SYNCED``, not ``DIVERGED`` — overwriting the
        mill's own commit is safe, and bailing there caused needless manual
        reconciliation after routine mill rebases.
    ``UNAVAILABLE`` — the remote couldn't be reached / inspected (fetch
        failed transiently, corrupt clone, etc.). Reconcile couldn't
        determine the relationship, but the lease ref was NOT advanced to
        any foreign commit, so ``push_with_lease`` still backstops a stale
        push. Callers may proceed (the lease catches a genuine race).
    """

    SYNCED = "synced"
    DIVERGED = "diverged"
    UNAVAILABLE = "unavailable"


def redact_credentials(text: str | bytes) -> str:
    """Strip ``user:token@`` userinfo from any URL embedded in *text*.

    Error paths that repr a failed git command (``CalledProcessError``
    includes the full argv) would otherwise echo the tokenized remote —
    ``https://oauth2:ghs_…@github.com/…`` — into ticket notes and
    Langfuse traces. Run every git-command error string through this
    before it leaves the process. Accepts bytes (CalledProcessError
    stderr is bytes when the command ran without ``text=True``)."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    return _CREDENTIAL_IN_URL.sub("://***@", text)


def _paths_from_diff(diff: str) -> list[str]:
    """Extract modified file paths from a unified git diff.

    Reads ``+++ b/<path>`` lines (skipping ``+++ /dev/null`` for deletions),
    deduplicates, preserves first-seen order. Used to pre-seed agent
    message histories with modified files so they don't pay one
    ``read_file`` round-trip per file.
    """
    _DIFF_PATH_RE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)
    seen: set[str] = set()
    out: list[str] = []
    for m in _DIFF_PATH_RE.finditer(diff):
        path = m.group(1).strip()
        if path and path != "/dev/null" and path not in seen:
            seen.add(path)
            out.append(path)
    return out


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


def _git_redacted(repo: Path, *args: str) -> str:
    """Like :func:`_git` but redacts credentials from any
    :class:`CalledProcessError` before propagation."""
    try:
        return _git(repo, *args)
    except subprocess.CalledProcessError as exc:
        raise subprocess.CalledProcessError(
            exc.returncode,
            [redact_credentials(str(a)) for a in exc.cmd],
            output=redact_credentials(exc.output or ""),
            stderr=redact_credentials(exc.stderr or ""),
        ) from None


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


def ls_remote_sha(
    remote_url: str, ref: str = "HEAD", token: str | None = None
) -> str | None:
    """Resolve *ref* on *remote_url* to a commit SHA without cloning.

    Runs ``git ls-remote`` against the remote and returns the SHA,
    or ``None`` on any failure (timeout, non-zero exit, unparseable
    output).  For private repos pass the forge *token* — it is
    injected into ``https://`` URLs via :func:`_authed_url`.
    """
    try:
        result = subprocess.run(
            ["git", "ls-remote", _authed_url(remote_url, token), ref],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception:
        return None

    if result.returncode != 0:
        return None

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        sha, _, _ = line.partition("\t")
        if sha:
            return sha

    return None


def branch_ancestry(repo: Path, branch: str, target: str) -> list[dict[str, str]]:
    """Return commits on ``origin/<branch>`` not on ``origin/<target>``.

    Each commit dict carries ``sha``, ``author_name``, ``author_email``,
    ``committer_name``, ``committer_email``, and ``subject``.  The agent
    calls this after a lease rejection to decide foreign-vs-self divergence:
    if every commit's author/committer is the mill itself it is a prior
    self-rebase and safe to retry; a foreign author means a human pushed
    and the mill must NOT clobber it.

    The caller must have already fetched both refs so ``origin/<branch>``
    and ``origin/<target>`` are current.  Returns an empty list when the
    two refs are identical or the remote branch doesn't exist.
    """
    try:
        out = _git(
            repo,
            "log",
            f"origin/{target}..origin/{branch}",
            "--format=%H|%an|%ae|%cn|%ce|%s",
        )
    except subprocess.CalledProcessError:
        return []
    if not out:
        return []
    commits: list[dict[str, str]] = []
    for line in out.split("\n"):
        parts = line.split("|", 5)
        if len(parts) >= 6:
            commits.append(
                {
                    "sha": parts[0],
                    "author_name": parts[1],
                    "author_email": parts[2],
                    "committer_name": parts[3],
                    "committer_email": parts[4],
                    "subject": parts[5],
                }
            )
    return commits


def create_branch(repo: Path, name: str) -> None:
    """Create or reset a branch (``git checkout -B``)."""
    _git(repo, "checkout", "-q", "-B", name)


def commit_all(repo: Path, message: str) -> None:
    """Stage all changes and commit (``git add -A`` + ``git commit -q -m``)."""
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def commit_file(repo: Path, filename: str, message: str) -> bool:
    """Stage ``filename`` and commit if it differs from HEAD.

    Returns True when a commit was created, False when the file was
    unchanged (nothing staged) — allowing callers to skip logging.

    Raises ``subprocess.CalledProcessError`` on git failure (caller is
    responsible for catching and warning).
    """
    _git(repo, "add", filename)
    result = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--quiet", "--", filename],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return False
    _git(repo, "commit", "-q", "-m", message)
    return True


def push(repo: Path, branch: str, remote_url: str, token: str | None) -> None:
    """Push ``branch`` to ``remote_url`` (token-auth for https). Uses
    ``--force`` so a re-delivery updates the bot-owned branch; pushes to
    the explicit authed URL rather than the clone's origin (the clone
    may have been made without a write token, and there is no
    remote-tracking ref to lease against on an explicit-URL push)."""
    _git_redacted(
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
    _git_redacted(
        repo,
        "fetch",
        _authed_url(remote_url, token),
        f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
    )


def _range_commit_emails(
    repo: Path, base: str, tip: str
) -> list[tuple[str, str]] | None:
    """Return ``[(author_email, committer_email)]`` for commits in
    ``base..tip`` (reachable from *tip* but not *base*).

    Returns ``None`` on any git error (caller treats undetermined
    authorship conservatively).
    """
    out = subprocess.run(
        ["git", "-C", str(repo), "log", "--format=%ae|%ce", f"{base}..{tip}"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return None
    pairs: list[tuple[str, str]] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        author, _, committer = line.partition("|")
        pairs.append((author, committer))
    return pairs


def reconcile_with_remote_pr(
    repo: Path, remote_url: str, branch: str, token: str | None
) -> ReconcileResult:
    """Fetch the remote PR branch and fast-forward the workspace clone
    to include any foreign commits (e.g. a human pushed a fix commit
    directly to the PR branch after the clone was created).

    Returns a :class:`ReconcileResult`:

    - ``SYNCED`` — already in sync, fast-forwarded, locally ahead, or the
      remote branch doesn't exist yet. Safe to proceed.
    - ``DIVERGED`` — both sides advanced independently; a force-push would
      silently overwrite the foreign commit and the lease can't protect
      it (see the enum docstring). Callers MUST block, not push.
    - ``UNAVAILABLE`` — the remote couldn't be fetched/inspected; the
      lease ref was not advanced to a foreign commit, so push_with_lease
      still backstops. Callers may proceed.
    """
    try:
        # 1. Update the remote-tracking ref.
        try:
            fetch(repo, remote_url=remote_url, token=token, branch=branch)
        except subprocess.CalledProcessError:
            # Fetch failed.  If we have no tracking ref at all the remote
            # branch likely doesn't exist yet → no-op success.
            if remote_branch_sha(repo, branch) is None:
                return ReconcileResult.SYNCED
            # Otherwise we couldn't refresh the ref — undetermined. The
            # lease ref was NOT advanced, so the push lease still guards.
            return ReconcileResult.UNAVAILABLE

        remote_sha = remote_branch_sha(repo, branch)
        if remote_sha is None:
            # Remote branch doesn't exist (unreachable after successful
            # fetch, but guard anyway).
            return ReconcileResult.SYNCED

        local_sha = head_sha(repo)
        if local_sha == remote_sha:
            return ReconcileResult.SYNCED  # Already in sync.

        # 2. If local is an ancestor of remote → fast-forward.
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "merge-base",
                "--is-ancestor",
                local_sha,
                remote_sha,
            ],
            capture_output=True,
        )
        if result.returncode == 0:
            _git(repo, "reset", "--hard", remote_sha)
            return ReconcileResult.SYNCED

        # 3. If remote is an ancestor of local → we're ahead, nothing to do.
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "merge-base",
                "--is-ancestor",
                remote_sha,
                local_sha,
            ],
            capture_output=True,
        )
        if result.returncode == 0:
            return ReconcileResult.SYNCED

        # 4. Neither is ancestor → diverged. A force-push would discard the
        # commits the remote carries that the local rebase does not
        # (``local..remote``). That is only unsafe when one of those discarded
        # commits is FOREIGN (a human pushed to the PR branch). When every
        # discarded commit is mill-authored, the "foreign" commit is just the
        # mill's OWN prior force-push from an earlier rebase cycle — safe to
        # overwrite. Distinguishing the two stops the false "diverged" bail
        # that otherwise forces a manual reconcile after every mill rebase.
        discarded = _range_commit_emails(repo, local_sha, remote_sha)
        if (
            discarded is not None
            and discarded
            and all(
                author in _MILL_EMAILS and committer in _MILL_EMAILS
                for author, committer in discarded
            )
        ):
            # Remote-unique commits are all the mill's own → push_with_lease
            # (leasing against the freshly-fetched origin ref) will overwrite
            # only mill commits. Safe to proceed.
            return ReconcileResult.SYNCED
        return ReconcileResult.DIVERGED
    except Exception:
        # Any unexpected git failure (missing repo, corrupt clone, etc.)
        # — undetermined; let the lease check provide the backstop.
        return ReconcileResult.UNAVAILABLE


def push_with_lease(
    repo: Path, branch: str, remote_url: str, token: str | None
) -> None:
    """Push ``branch`` to ``remote_url`` with a compare-and-swap lease.

    Uses ``--force-with-lease=<branch>:<expected-sha>`` where
    ``<expected-sha>`` is the current ``refs/remotes/origin/<branch>``
    value (which must have been populated by a prior ``fetch()`` or
    ``reconcile_with_remote_pr()`` call).  If the remote branch doesn't
    exist yet (``remote_branch_sha`` returns ``None``), falls back to a
    plain ``--force`` push — there is nothing to lease against.

    A lease violation raises :class:`subprocess.CalledProcessError` (git
    exits non-zero).  The existing ``except Exception`` blocks in the
    callers already catch this and route to BLOCKED.
    """
    expected_sha = remote_branch_sha(repo, branch)
    if expected_sha is None:
        # Remote branch doesn't exist yet — nothing to lease against.
        _git_redacted(
            repo,
            "push",
            "--force",
            _authed_url(remote_url, token),
            f"{branch}:{branch}",
        )
    else:
        _git_redacted(
            repo,
            "push",
            f"--force-with-lease=refs/heads/{branch}:{expected_sha}",
            _authed_url(remote_url, token),
            f"{branch}:{branch}",
        )


class PostPushResult(str, Enum):
    """Outcome of :func:`post_push_check`.

    ``PASS`` — the push landed, no foreign commits clobbered, and the
        remote branch is in a safe state.
    ``NOT_LANDED`` — the remote HEAD does not match the local HEAD; the
        agent's push did not actually land on the remote.
    ``FOREIGN_DIVERGENCE`` — the remote branch carries commits ahead of
        the target that are NOT attributable to the mill (foreign
        authorship).  The push may have clobbered a human commit.
    ``UNAVAILABLE`` — the remote could not be reached (fetch failed
        transiently, etc.).  Callers should re-poll rather than block.
    """

    PASS = "pass"  # noqa: S105 — enum value, not a credential
    NOT_LANDED = "not_landed"
    FOREIGN_DIVERGENCE = "foreign_divergence"
    UNAVAILABLE = "unavailable"


_MILL_EMAILS: frozenset[str] = frozenset({"mill@robotsix.local"})


def post_push_check(
    repo: Path,
    branch: str,
    target: str,
    remote_url: str,
    token: str | None,
) -> PostPushResult:
    """Deterministic post-check after an agent-driven push.

    1. Fetches the remote PR branch and refreshes ``origin/<target>``.
    2. Verifies the remote branch HEAD == the local HEAD (the push
       actually landed).
    3. Verifies every commit the remote branch carries ahead of
       ``origin/<target>`` is attributable to the mill (no foreign
       authorship — nothing was clobbered).

    Returns a :class:`PostPushResult`.  This is a pure host-side check
    with no LLM involvement — it runs AFTER the agent reports DONE.
    """
    # 1. Fetch both refs so comparisons are current.
    try:
        fetch(repo, remote_url=remote_url, token=token, branch=branch)
        fetch(repo, remote_url=remote_url, token=token, branch=target)
    except subprocess.CalledProcessError:
        return PostPushResult.UNAVAILABLE

    # 2. Remote HEAD must equal local HEAD.
    try:
        local = head_sha(repo)
    except subprocess.CalledProcessError:
        return PostPushResult.UNAVAILABLE
    remote = remote_branch_sha(repo, branch)
    if remote is None or local != remote:
        return PostPushResult.NOT_LANDED

    # 3. Every ahead-of-target commit must be mill-authored.
    commits = branch_ancestry(repo, branch, target)
    for c in commits:
        author = c.get("author_email", "")
        committer = c.get("committer_email", "")
        if author not in _MILL_EMAILS or committer not in _MILL_EMAILS:
            return PostPushResult.FOREIGN_DIVERGENCE

    return PostPushResult.PASS


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
    try:
        diff_out = _git(repo, "diff", "--name-only", f"origin/{target_branch}")
    except subprocess.CalledProcessError:
        log.warning(
            "changed_files: origin/%s ref not resolvable in %s — "
            "treating as no tracked diff available",
            target_branch,
            repo,
        )
        diff_out = ""
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

    def _collect(git_output: str) -> None:
        for f in git_output.split("\n"):
            if f and f not in seen_set:
                seen_set.add(f)
                seen.append(f)

    seen: list[str] = []
    seen_set: set[str] = set()
    try:
        committed_out = _git(
            repo, "diff", "--name-only", f"origin/{target_branch}...HEAD"
        )
    except subprocess.CalledProcessError:
        log.warning(
            "introduced_files: origin/%s ref not resolvable in %s — "
            "treating as no branch-introduced diff available",
            target_branch,
            repo,
        )
        committed_out = ""
    if committed_out:
        _collect(committed_out)
    working_out = _git(repo, "diff", "--name-only", "HEAD")
    if working_out:
        _collect(working_out)
    untracked_out = _git(repo, "ls-files", "--others", "--exclude-standard")
    if untracked_out:
        _collect(untracked_out)
    return seen


def added_files(repo: Path, target_branch: str) -> list[str]:
    """Return every file the BRANCH ADDS (git status ``A``) relative to
    its merge base with ``origin/<target_branch>``.

    Uses ``git diff --name-status --diff-filter=A
    origin/<target>...HEAD`` (THREE-dot, against the merge base) so files
    that ``origin/<target>`` independently added after the branch was cut
    do NOT appear — only brand-new files the branch itself introduces.
    Modified / deleted / renamed paths are excluded.
    """
    out = _git(
        repo,
        "diff",
        "--name-status",
        "--diff-filter=A",
        f"origin/{target_branch}...HEAD",
    )
    added: list[str] = []
    if out:
        for line in out.split("\n"):
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0].startswith("A") and parts[-1]:
                added.append(parts[-1])
    return added


def conflicted_files(repo: Path) -> list[str]:
    """Return the paths with unresolved merge conflicts (git status ``U``).

    Uses ``git diff --name-only --diff-filter=U``, which lists unmerged
    paths during an in-progress merge/rebase. Returns ``[]`` when there
    are none (clean tree, or the rebase was already aborted). Best-effort:
    any git error degrades to ``[]`` so failure reporting never crashes.
    """
    try:
        out = _git(repo, "diff", "--name-only", "--diff-filter=U")
    except Exception:  # noqa: BLE001 — diagnostics must not fail the caller
        return []
    return [line for line in out.split("\n") if line] if out else []


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
