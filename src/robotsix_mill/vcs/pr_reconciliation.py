"""PR-reconciliation logic extracted from ``git_ops`` (module size).

These helpers decide whether the mill may safely force-push a rebased PR
branch, or whether a foreign (human) commit would be clobbered. They lean
on the low-level git wrappers in :mod:`.git_ops`, imported one-directionally
at module top; ``git_ops`` re-exports the public names at its own bottom so
external callers keep using ``git_ops.reconcile_with_remote_pr`` etc.
"""

from __future__ import annotations

import subprocess
from enum import StrEnum
from pathlib import Path

# Bind the git_ops MODULE (not its individual functions) so the low-level
# seams (``fetch``, ``head_sha``, ``remote_branch_sha``, ``branch_ancestry``)
# are resolved as ``git_ops.<name>`` at call time.  These functions lived in
# git_ops before the module-size split, where tests monkeypatch them via
# ``git_ops.<name>``; a ``from .git_ops import fetch`` here would bind a
# private copy that ignores those patches (regressing the fetch-before-rebase
# tests).  Constants/enums and truly-internal helpers are not patched, so they
# stay as direct imports.
from . import git_ops
from .git_ops import (
    NETWORK_GIT_TIMEOUT,
    ReconcileResult,
    _authed_url,
    _git,
    _git_redacted,
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
            git_ops.fetch(repo, remote_url=remote_url, token=token, branch=branch)
        except subprocess.CalledProcessError:
            # Fetch failed.  If we have no tracking ref at all the remote
            # branch likely doesn't exist yet → no-op success.
            if git_ops.remote_branch_sha(repo, branch) is None:
                return ReconcileResult.SYNCED
            # Otherwise we couldn't refresh the ref — undetermined. The
            # lease ref was NOT advanced, so the push lease still guards.
            return ReconcileResult.UNAVAILABLE

        remote_sha = git_ops.remote_branch_sha(repo, branch)
        if remote_sha is None:
            # Remote branch doesn't exist (unreachable after successful
            # fetch, but guard anyway).
            return ReconcileResult.SYNCED

        local_sha = git_ops.head_sha(repo)
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
        # discarded commit is automation-authored, the "foreign" commit is
        # just the mill's OWN prior push from an earlier cycle — safe to
        # overwrite. Distinguishing the two stops the false "diverged" bail
        # that otherwise forces a manual reconcile after every mill rebase.
        #
        # Must use :func:`_is_automation_identity`, not ``_MILL_EMAILS``:
        # the mill pushes under two identities. Local git commits are
        # ``mill@robotsix.local``, but anything pushed through the GitHub App
        # (every ci_fix push) is authored by
        # ``<id>+robotsix-mill[bot]@users.noreply.github.com``. Matching only
        # the former made the mill block on its OWN App commits with "a human
        # likely pushed" — the same identity gap fixed in #2663 for
        # post_push_check, which this site did not inherit.
        discarded = _range_commit_emails(repo, local_sha, remote_sha)
        if (
            discarded is not None
            and discarded
            and all(
                _is_automation_identity(author) and _is_automation_identity(committer)
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
    expected_sha = git_ops.remote_branch_sha(repo, branch)
    if expected_sha is None:
        # Remote branch doesn't exist yet — nothing to lease against.
        _git_redacted(
            repo,
            "push",
            "--force",
            _authed_url(remote_url, token),
            f"{branch}:{branch}",
            timeout=NETWORK_GIT_TIMEOUT,
        )
    else:
        _git_redacted(
            repo,
            "push",
            f"--force-with-lease=refs/heads/{branch}:{expected_sha}",
            _authed_url(remote_url, token),
            f"{branch}:{branch}",
            timeout=NETWORK_GIT_TIMEOUT,
        )


class PostPushResult(StrEnum):
    """Outcome of :func:`post_push_check`.

    ``PASS`` — the push landed, no foreign commits clobbered, and the
        remote branch is in a safe state.
    ``NOT_LANDED`` — the remote HEAD does not match the local HEAD; the
        agent's push did not actually land on the remote.
    ``FOREIGN_DIVERGENCE`` — the remote branch carries commits ahead of
        the target that are NOT attributable to automation (the mill, a
        GitHub App, or an Action).  The push may have clobbered a human
        commit.
    ``UNAVAILABLE`` — the remote could not be reached (fetch failed
        transiently, etc.).  Callers should re-poll rather than block.
    """

    PASS = "pass"
    NOT_LANDED = "not_landed"
    FOREIGN_DIVERGENCE = "foreign_divergence"
    UNAVAILABLE = "unavailable"


_MILL_EMAILS: frozenset[str] = frozenset({"mill@robotsix.local"})

# GitHub attributes automation commits to identities that are not
# ``mill@robotsix.local`` but are still emphatically not a human:
#
#   github-actions[bot]@users.noreply.github.com   repo CI (e.g. auto-format)
#   285582353+robotsix-mill[bot]@users.noreply…    mill's OWN GitHub App
#   noreply@github.com                             committer on API merges
#
# Treating these as foreign made the post-push check report "a human likely
# pushed to the PR branch" for mill's own CI and mill's own bot, and blocked
# the ticket. Every GitHub App and Action shares the ``[bot]@users.noreply``
# suffix, so match on that rather than enumerating app ids that change per
# installation.
_BOT_EMAIL_SUFFIX = "[bot]@users.noreply.github.com"
_GITHUB_API_COMMITTER = "noreply@github.com"


def _is_automation_identity(email: str) -> bool:
    """True when *email* belongs to the mill, a GitHub App, or an Action.

    The post-push check exists to catch a *human* commit being clobbered.
    Anything matching here is automation, so it is not the divergence the
    check is guarding against.
    """
    return (
        email in _MILL_EMAILS
        or email.endswith(_BOT_EMAIL_SUFFIX)
        or email == _GITHUB_API_COMMITTER
    )


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
       ``origin/<target>`` is attributable to automation — the mill, a
       GitHub App, or an Action (no *human* authorship, so nothing a
       person wrote was clobbered).

    Returns a :class:`PostPushResult`.  This is a pure host-side check
    with no LLM involvement — it runs AFTER the agent reports DONE.
    """
    # 1. Fetch both refs so comparisons are current.
    try:
        git_ops.fetch(repo, remote_url=remote_url, token=token, branch=branch)
        git_ops.fetch(repo, remote_url=remote_url, token=token, branch=target)
    except subprocess.CalledProcessError:
        return PostPushResult.UNAVAILABLE

    # 2. Remote HEAD must equal local HEAD.
    try:
        local = git_ops.head_sha(repo)
    except subprocess.CalledProcessError:
        return PostPushResult.UNAVAILABLE
    remote = git_ops.remote_branch_sha(repo, branch)
    if remote is None or local != remote:
        return PostPushResult.NOT_LANDED

    # 3. Every ahead-of-target commit must come from automation, not a human.
    commits = git_ops.branch_ancestry(repo, branch, target)
    for c in commits:
        author = c.get("author_email", "")
        committer = c.get("committer_email", "")
        if not _is_automation_identity(author) or not _is_automation_identity(
            committer
        ):
            return PostPushResult.FOREIGN_DIVERGENCE

    return PostPushResult.PASS
