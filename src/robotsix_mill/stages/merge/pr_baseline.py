"""Shared PR preamble and auto-merge eligibility for the merge stage.

:class:`PrBaselineMixin` holds the helpers common to every merge poll
path rather than any single one:

- ``_check_pr_baseline`` — the shared PR-status preamble (merged / closed
  / missing handling) used by the IMPLEMENT_COMPLETE, HUMAN_MR_APPROVAL
  and WAITING_AUTO_MERGE polls.
- ``_closed_pr_branch_is_empty`` — the net-diff check that decides whether
  a closed-unmerged PR is a genuine no-op.
- ``_auto_merge_eligible`` — the config-and-forge gate that every path
  consults before an autonomous merge.

These methods resolve at runtime through the assembled
:class:`~.core.MergeStage` MRO and are declared for the type checker on
:class:`~._base._MergeStageBase`; this module imports no sibling mixin.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

from ...config import target_branch_for
from ...core.models import SourceKind, Ticket
from ...core.states import State
from ...forge import Forge, get_forge
from ..base import Outcome, StageContext
from ._base import _MergeStageBase
from ._shared import (
    _PR_MISSING_COUNT,
    _next_consecutive,
    _reset_consecutive,
    _verify_merge_ancestor,
    log,
)


def _extract_tracked_pr_url(description: str) -> str | None:
    """Extract the tracked PR URL from a tracker ticket description.

    Looks for the line ``- URL: <url>`` written by ``_file_foreign_ticket`` /
    ``_file_orphan_ticket``.  Returns ``None`` when not found.
    """
    m = re.search(r"- URL: (https://[^\s]+)", description)
    return m.group(1) if m else None


class PrBaselineMixin(_MergeStageBase):
    """Shared PR preamble and auto-merge eligibility gate for the merge stage."""

    def _check_pr_baseline(
        self,
        ticket: Ticket,
        ctx: StageContext,
        branch: str,
        same_state: State,
        *,
        verify_merge: bool = False,
    ) -> tuple[dict[str, Any] | None, Outcome | None]:
        """Shared PR preamble: fetch status & handle merged/closed/None/error.

        Returns ``(pr, None)`` when the PR is open and not merged/closed.
        Returns ``(None, outcome)`` for early-return cases:
        - error fetching PR → *same_state*
        - no PR found → *same_state*
        - PR merged → ``State.DONE``
        - PR closed → ``State.BLOCKED``

        When *verify_merge* is True and the PR is reported merged, the
        helper confirms the merge is actually present on the target branch
        (``_verify_merge_ancestor``).  If the verification fails the
        outcome is ``State.IMPLEMENT_COMPLETE`` instead of DONE.
        """
        s = ctx.settings
        try:
            pr = get_forge(s, repo_config=ctx.repo_config).pr_status(
                source_branch=branch
            )
        except Exception as e:
            log.warning("%s: PR status check failed (retry): %s", ticket.id, e)
            return None, Outcome(same_state)

        if pr is None:
            # For tracker tickets, the mill branch may have been deleted or
            # never had a PR.  Fall back to checking the tracked PR by URL.
            if ticket.source == SourceKind.ORPHANED_PR_CHECK:
                description = ctx.service.workspace(ticket).read_description()
                tracked_url = _extract_tracked_pr_url(description)
                if tracked_url:
                    try:
                        tracked = get_forge(
                            s, repo_config=ctx.repo_config
                        ).pr_status_by_url(url=tracked_url)
                    except Exception as exc:
                        log.warning(
                            "%s: tracked PR status check failed: %s",
                            ticket.id,
                            exc,
                        )
                        tracked = None
                    if tracked is not None:
                        if tracked.get("merged"):
                            return None, Outcome(
                                State.BLOCKED,
                                f"Tracked PR merged ({tracked_url}) — "
                                "reconcile pass will close",
                            )
                        if tracked.get("state") == "closed":
                            return None, Outcome(
                                State.BLOCKED,
                                f"Tracked PR closed ({tracked_url}) — "
                                "reconcile pass will close",
                            )
                        # Tracked PR resolved and is still OPEN — a
                        # legitimately-waiting foreign-PR tracker awaiting an
                        # external merge, NOT a dead lookup.  Re-poll
                        # indefinitely (the design before the no-PR spin
                        # guard) without touching the consecutive no-PR
                        # counter, which would otherwise BLOCK a live tracker
                        # with a misleading "branch never pushed" note.
                        return None, Outcome(same_state)
            if pr is None:
                # No PR found for the branch in the board-derived repo (and
                # no tracked-URL fallback applied).  Count consecutive
                # same-reason passes and escalate to BLOCKED past the
                # ceiling instead of silently re-polling the same dead
                # lookup forever — a cross-repo/meta deliver records the
                # real PR URL on the ticket, so a branch-keyed lookup
                # against the wrong repo can never succeed (observed
                # 2026-09-03: a meta ticket spun in IMPLEMENT_COMPLETE
                # > 26h on "No PR found for this branch").
                max_polls = s.merge_pr_missing_max_polls
                if max_polls:
                    artifacts_dir = ctx.service.workspace(ticket).artifacts_dir
                    if _next_consecutive(artifacts_dir, _PR_MISSING_COUNT) >= max_polls:
                        return None, Outcome(
                            State.BLOCKED,
                            f"no PR found for branch {branch!r} for {max_polls} "
                            "consecutive merge polls — the PR may live in a repo "
                            "other than this board's repo (check the recorded PR "
                            "URL on the ticket) or the branch was never pushed; "
                            "manual intervention required",
                        )
                log.info(
                    "%s: no PR found for branch %r (may be in a different repo) — "
                    "re-polling",
                    ticket.id,
                    branch,
                )
                return None, Outcome(same_state)
        # A PR was found — any previous consecutive no-PR streak is over.
        _reset_consecutive(
            ctx.service.workspace(ticket).artifacts_dir, _PR_MISSING_COUNT
        )
        if pr.get("merged"):
            if verify_merge:
                from robotsix_mill.stages import merge as _facade

                sha = pr.get("sha", "")
                repo_dir = _facade._workspace_repo_dir(ctx, ticket)
                target = target_branch_for(s, ctx.repo_config)
                if not _verify_merge_ancestor(repo_dir, sha, ticket.id, target):
                    log.warning(
                        "%s: PR reported merged but commit %s is not an ancestor of "
                        "origin/%s — falling back to IMPLEMENT_COMPLETE for investigation",
                        ticket.id,
                        sha[:8] if sha else "(none)",
                        target,
                    )
                    return None, Outcome(
                        State.IMPLEMENT_COMPLETE,
                        f"PR reported merged but merge not confirmed on origin/{target}: {pr.get('url', '')}",
                    )
            ctx.service.workspace(ticket).artifacts_dir.joinpath("merge.md").write_text(
                f"merged: {pr.get('url', '')}\n", encoding="utf-8"
            )
            self._cleanup_branch_on_done(ticket, ctx, branch)
            log.info("%s: PR merged → done", ticket.id)
            return None, Outcome(State.DONE, f"merged: {pr.get('url', '')}")
        if pr.get("state") == "closed":
            # A PR closed without merge normally means a human (or the
            # forge) rejected it — resumable BLOCKED. BUT when the branch
            # has no net diff vs the target (empty-after-rebase: main
            # already carries the change), there is nothing left to
            # merge and re-queueing would loop forever. Terminate DONE in
            # that genuine-no-op case. The net-diff check fetches origin
            # and fails safe to "has diff" → BLOCKED, so a real change is
            # never silently closed.
            if self._closed_pr_branch_is_empty(ticket, ctx, branch):
                log.info(
                    "%s: PR closed without merge and branch is empty vs target "
                    "→ DONE (already satisfied)",
                    ticket.id,
                )
                ctx.service.workspace(ticket).artifacts_dir.joinpath(
                    "merge.md"
                ).write_text(f"closed-empty: {pr.get('url', '')}\n", encoding="utf-8")
                self._cleanup_branch_on_done(ticket, ctx, branch)
                return None, Outcome(
                    State.DONE,
                    "already satisfied — PR closed with an empty branch (no "
                    f"changes to merge): {pr.get('url', '')}",
                )
            return None, Outcome(
                State.BLOCKED,
                f"PR closed without merge — resumable: {pr.get('url', '')}",
            )

        return pr, None

    def _closed_pr_branch_is_empty(
        self, ticket: Ticket, ctx: StageContext, branch: str
    ) -> bool:
        """Return True iff the ticket's branch has no net diff vs the target.

        Best-effort and fail-safe: returns False (→ keep BLOCKED) whenever
        emptiness cannot be positively confirmed (no workspace clone,
        branch ref missing, git error). Only a confirmed empty net diff
        vs ``origin/<target>`` returns True, so a PR that was closed while
        still carrying real changes is never silently marked DONE.
        """
        from robotsix_mill.stages import merge as _facade

        from ...vcs import git_ops

        repo_dir = _facade._workspace_repo_dir(ctx, ticket)
        if repo_dir is None:
            return False
        repo_path = Path(repo_dir)
        # Resolve the branch ref: prefer the local branch, fall back to
        # HEAD only if the branch ref is unavailable.
        ref = branch if git_ops.branch_exists(repo_path, branch) else "HEAD"
        target = target_branch_for(ctx.settings, ctx.repo_config)
        try:
            return not git_ops.branch_has_net_diff(repo_path, target, ref=ref)
        except Exception:
            return False

    def _auto_merge_eligible(
        self,
        ticket: Ticket,
        ctx: StageContext,
        pr_head_sha: str | None = None,
        *,
        forge: Forge | None = None,
        pr: dict[str, Any] | None = None,
        skip_sensitive_path_check: bool = False,
    ) -> tuple[bool, str]:
        """Return ``(eligible, reason)`` for auto-merge.

        *eligible* is True when ALL of the following hold:
        1. Global ``settings.auto_merge_enabled`` is True
        2. ``settings.auto_merge_kill_switch`` is False
        3. ``settings.review_enabled`` is True
        4. Repo is NOT in ``settings.auto_merge_infra_denylist``
        5. PR author is the mill/chat agent (only when *forge* + *pr* given)
        6. No sensitive paths touched (only when *forge* + *pr* given,
           and *skip_sensitive_path_check* is False)

        When *forge* + *pr* are not provided, only config-only gates
        (1–4) are evaluated.  Provide both to enable the forge-dependent
        safety checks (5–6).  The review artifact is no longer required.

        *skip_sensitive_path_check* bypasses gate 6.  This lets callers
        that have already confirmed the diff was human-approved skip the
        sensitive-path check after a no-op rebase.

        The per-repo ``auto_merge_enabled`` toggle is NOT checked here —
        it controls routing (autonomous merge vs. human-in-the-loop), not
        whether mill is *allowed* to merge.

        *reason* explains the blocking condition when eligible is False.
        """
        s = ctx.settings
        rc = ctx.repo_config
        if rc is None:
            return False, "no repo config available"

        # --- Config-only gates (fast, no I/O) ---
        if not s.auto_merge_enabled:
            return False, "auto-merge disabled in global config"
        if s.auto_merge_kill_switch:
            return False, "auto-merge kill-switch is active"
        if not s.review_enabled:
            return False, "review gate disabled — human approval required"
        if rc.repo_id in s.auto_merge_infra_denylist:
            return False, f"repo {rc.repo_id!r} is on the infra denylist"

        # --- Forge-dependent safety gates ---
        if forge is not None and pr is not None:
            # 5. PR author must be the mill/chat agent.
            author = (pr or {}).get("author", "")
            if author:
                bot_logins = s.orphaned_pr_bot_logins or []
                if bot_logins:
                    allowed = set(bot_logins)
                else:
                    bot_login = forge.get_authenticated_user_login()
                    allowed = {bot_login} if bot_login else set()
                if allowed and author not in allowed:
                    return False, (
                        f"PR author {author!r} is not a trusted bot; "
                        f"allowed: {sorted(allowed)}"
                    )

            # 6. No sensitive paths touched.
            if not skip_sensitive_path_check:
                try:
                    files = forge.pr_files(source_branch=ticket.branch or "")
                except Exception:
                    # Fail-closed: any error fetching files → not eligible.
                    return False, "failed to fetch PR file list"
                sensitive = s.auto_merge_sensitive_globs or []
                for f in files:
                    path = f.get("path", "")
                    for pattern in sensitive:
                        if fnmatch.fnmatch(path, pattern):
                            return False, (
                                f"sensitive path touched: {path!r} "
                                f"(matched glob {pattern!r})"
                            )

        return True, "eligible"
