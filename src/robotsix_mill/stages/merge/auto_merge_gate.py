"""WAITING_AUTO_MERGE polling: the final auto-merge gate.

Isolated from :mod:`.ci_poll` because it is the one polling path that runs
*after* merge eligibility has already been decided by earlier stages — it
only re-polls CI and triggers auto-merge when the branch is green. It does
not participate in the IMPLEMENT_COMPLETE or HUMAN_MR_APPROVAL routing.

The path is a single method on :class:`AutoMergeGateMixin`, which
:class:`~.core.MergeStage` inherits alongside :class:`~.ci_poll.CIPollMixin`.
The shared eligibility/merge helpers it calls (``_check_pr_baseline``,
``_auto_merge_eligible``, ``_try_auto_merge``, …) live on ``CIPollMixin``
and resolve at runtime through the assembled ``MergeStage`` MRO — declared
for the type checker on :class:`~._base._MergeStageBase`.
"""

from __future__ import annotations

from typing import cast

from ...core.models import Ticket
from ...core.states import State
from ...forge import get_forge
from ..base import Outcome, StageContext
from ._base import _MergeStageBase
from ._shared import (
    _REBASE_FROM_STATE,
    _ci_truly_green,
    log,
)


class AutoMergeGateMixin(_MergeStageBase):
    """Final auto-merge gate: re-poll CI for a WAITING_AUTO_MERGE ticket."""

    def _poll_waiting_auto_merge(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Re-poll CI for a ticket in WAITING_AUTO_MERGE.

        The ticket was already determined eligible for auto-merge; CI was
        pending. On each poll:
        - CI success → try auto-merge (DONE or HUMAN_MR_APPROVAL on forge reject)
        - CI failure → FIXING_CI
        - CI green but branch behind target → IMPLEMENT_COMPLETE (the gate
          check dispatches the rebase agent to catch the branch up)
        - CI still pending → WAITING_AUTO_MERGE (same-state no-op)
        - Eligibility lost → HUMAN_MR_APPROVAL with comment
        """
        s = ctx.settings
        branch = ticket.branch or f"{s.branch_prefix}{ticket.id}"

        pr, early = self._check_pr_baseline(
            ticket, ctx, branch, State.WAITING_AUTO_MERGE, verify_merge=True
        )
        if early is not None:
            return cast(Outcome, early)
        if pr is None:  # type guard: _check_pr_baseline guarantees pr is non-None here
            raise RuntimeError("_check_pr_baseline returned (None, None) — impossible")

        # Re-check eligibility (review artifact may have changed / become stale).
        feature_tip_sha = pr.get("sha", "")
        forge = get_forge(s, repo_config=ctx.repo_config)
        eligible, reason = self._auto_merge_eligible(
            ticket, ctx, pr_head_sha=feature_tip_sha, forge=forge, pr=pr
        )
        if not eligible:
            self._maybe_comment(ticket, ctx, reason)
            return Outcome(State.HUMAN_MR_APPROVAL, reason)

        mergeable = pr.get("mergeable")
        if mergeable is False:
            # PR became conflicting while waiting — try the server-side
            # update-branch API first (merges base into head) before
            # falling back to the heavy rebase agent.
            if self._try_update_branch_for_conflict(ticket, ctx, branch):
                return Outcome(State.WAITING_AUTO_MERGE)
            # update-branch failed — genuine content conflict.
            if not s.autonomous_rebase_enabled:
                # Autonomous rebase disabled — fall back to the legacy
                # IMPLEMENT_COMPLETE path.
                log.info(
                    "%s: PR became conflicting while waiting for CI → IMPLEMENT_COMPLETE (autonomous rebase disabled)",
                    ticket.id,
                )
                return Outcome(
                    State.IMPLEMENT_COMPLETE,
                    "PR is now conflicting; gates no longer pass",
                )
            # Route directly to REBASING so the PR is auto-rebased
            # and returned to the merge loop without operator action.
            artifacts_dir = ctx.service.workspace(ticket).artifacts_dir
            artifacts_dir.joinpath(_REBASE_FROM_STATE).write_text(
                State.WAITING_AUTO_MERGE.value, encoding="utf-8"
            )
            log.info(
                "%s: PR became conflicting while waiting for CI → REBASING",
                ticket.id,
            )
            return Outcome(
                State.REBASING, "PR is now conflicting; rebasing automatically"
            )

        # --- Review feedback check (opt-in): a late CHANGES_REQUESTED must
        # short-circuit to ADDRESSING_REVIEW before any auto-merge. ---
        review_outcome = self._review_changes_requested_outcome(
            ticket,
            ctx,
            branch=branch,
            forge=get_forge(s, repo_config=ctx.repo_config),
            pr_head_sha=pr.get("sha", ""),
        )
        if review_outcome is not None:
            return cast(Outcome, review_outcome)

        # Check CI.
        try:
            ci_status = forge.check_status(source_branch=branch)
        except Exception as e:
            log.warning("%s: check_status failed (retry): %s", ticket.id, e)
            return Outcome(State.WAITING_AUTO_MERGE)

        if ci_status is None:
            # No CI data yet — keep waiting.
            self._maybe_comment(ticket, ctx, "CI pending — will auto-merge when green")
            return Outcome(State.WAITING_AUTO_MERGE)

        conclusion = ci_status.get("conclusion")
        if conclusion == "failure":
            log.info(
                "%s: CI failed while waiting for auto-merge → IMPLEMENT_COMPLETE",
                ticket.id,
            )
            self._maybe_comment(ticket, ctx, "CI failed — falling back to gate check")
            return Outcome(State.IMPLEMENT_COMPLETE, "CI failed; gates no longer pass")

        if _ci_truly_green(conclusion, pr):
            # CI is green AND the forge's combined view is clean — attempt
            # auto-merge. Gating on _ci_truly_green (not bare conclusion)
            # prevents merging on a premature green: after a force-push the
            # fast checks can report success before the slow required gate
            # starts, with mergeable_state still "blocked"/"behind" — merging
            # then would redden the target branch.
            return cast(
                Outcome,
                self._try_auto_merge(
                    ticket,
                    ctx,
                    pr,
                    branch,
                    pr.get("sha", ""),
                    forge,
                    merge_fail_same_state=State.WAITING_AUTO_MERGE,
                    verify_fail_state=State.IMPLEMENT_COMPLETE,
                ),
            )

        if conclusion == "success" and pr.get("mergeable_state") == "behind":
            # Green CI on a stale head — try the server-side update-branch
            # API to bring the PR head current without a full local rebase.
            log.info(
                "%s: CI green but branch behind target — attempting update-branch",
                ticket.id,
            )
            update_result = forge.update_branch(source_branch=branch)
            if update_result.get("updated"):
                self._maybe_comment(
                    ticket,
                    ctx,
                    "Branch was behind target; auto-updated via update-branch API. "
                    "Waiting for CI to re-run before retrying auto-merge.",
                )
                return Outcome(State.WAITING_AUTO_MERGE)
            self._maybe_comment(
                ticket,
                ctx,
                f"Branch is behind target but update-branch failed: "
                f"{update_result.get('reason', 'unknown')} — falling back to "
                f"IMPLEMENT_COMPLETE",
            )
            return Outcome(
                State.IMPLEMENT_COMPLETE,
                f"branch behind target; update-branch failed: "
                f"{update_result.get('reason', 'unknown')}",
            )

        # Pending or None — keep waiting.
        self._maybe_comment(ticket, ctx, "CI pending — will auto-merge when green")
        return Outcome(State.WAITING_AUTO_MERGE)
