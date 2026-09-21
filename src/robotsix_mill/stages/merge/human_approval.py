"""HumanApprovalMixin: the HUMAN_MR_APPROVAL poll path for the merge stage.

Polls PR status for a ticket parked awaiting human merge approval:
handles merged/closed/conflicting states, re-checks CI, and triggers the
autonomous merge (or the update-branch / rebase fallbacks) when the PR is
green and eligible.

The shared eligibility/preamble helpers it calls (``_check_pr_baseline``,
``_auto_merge_eligible``) live on :class:`~.pr_baseline.PrBaselineMixin`
and the review-feedback check on
:class:`~.review_revision.ReviewRevisionMixin`; all resolve at runtime
through the assembled :class:`~.core.MergeStage` MRO and are declared for
the type checker on :class:`~._base._MergeStageBase`.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path
from typing import Any, cast

from ...config import target_branch_for
from ...core.models import Ticket
from ...core.states import State
from ...forge import Forge, get_forge
from ..base import Outcome, StageContext
from ._base import _MergeStageBase
from ._shared import (
    _APPROVED_DIFF_HASH,
    _REBASE_COUNTER,
    _REBASE_FROM_STATE,
    _REBASE_LAST_TS,
    _ci_truly_green,
    _merge_rejection_outcome,
    _verify_merge_ancestor,
    _write_counter,
    log,
)


def _within_rebase_cooldown(artifacts_dir: Path, cooldown_hours: int) -> bool:
    """Return True when the last successful rebase is still within the cooldown window.

    Used by ``_handle_human_mr_approval`` to avoid continuously re-rebasing
    PRs parked for human approval.  When *cooldown_hours* is 0 the feature
    is disabled and this always returns False.  Missing or unparseable
    timestamps are treated as "no throttle" (the timestamp may not exist
    yet, e.g. before the first rebase).
    """
    if cooldown_hours <= 0:
        return False
    ts_path = artifacts_dir / _REBASE_LAST_TS
    try:
        ts_text = ts_path.read_text(encoding="utf-8").strip()
        last = datetime.datetime.fromisoformat(ts_text)
        age = datetime.datetime.now(datetime.UTC) - last
        return age.total_seconds() < cooldown_hours * 3600
    except OSError, ValueError:
        return False


class HumanApprovalMixin(_MergeStageBase):
    """HUMAN_MR_APPROVAL polling: merged/closed/conflicting/CI/auto-merge."""

    def _try_auto_merge(
        self,
        ticket: Ticket,
        ctx: StageContext,
        pr: dict[str, Any],
        branch: str,
        feature_tip_sha: str,
        forge: Forge,
        *,
        merge_fail_same_state: State,
        verify_fail_state: State,
    ) -> Outcome:
        """Execute ``forge.merge_pr`` and handle all success/failure paths.

        Caller must have already verified CI is truly green and the PR is
        eligible for auto-merge.

        On success: writes ``merge.md`` artifact, cleans up branch, returns
        ``DONE``.  On merge-ancestor verification failure: returns
        *verify_fail_state*.  On forge rejection: delegates to
        ``_merge_rejection_outcome`` with *merge_fail_same_state* as the
        same-state fallback.
        """
        from robotsix_mill.stages import merge as _facade

        s = ctx.settings
        result = forge.merge_pr(source_branch=branch)
        if result.get("merged"):
            repo_dir = _facade._workspace_repo_dir(ctx, ticket)
            target = target_branch_for(s, ctx.repo_config)
            if not _verify_merge_ancestor(repo_dir, feature_tip_sha, ticket.id, target):
                log.warning(
                    "%s: auto-merge reported success but commit %s is not an "
                    "ancestor of origin/%s — falling back to %s",
                    ticket.id,
                    feature_tip_sha[:8] if feature_tip_sha else "(none)",
                    target,
                    verify_fail_state.value,
                )
                return Outcome(
                    verify_fail_state,
                    f"auto-merge reported success but merge not confirmed on origin/{target}",
                )
            ctx.service.workspace(ticket).artifacts_dir.joinpath("merge.md").write_text(
                f"auto-merged: {pr.get('url', '')}\n",
                encoding="utf-8",
            )
            self._cleanup_branch_on_done(ticket, ctx, branch)
            log.info("%s: auto-merged → done", ticket.id)
            return Outcome(
                State.DONE,
                f"auto-merged: {pr.get('url', '')}",
            )
        # Forge rejected the merge — retry a bounded number of
        # passes when it may just be a required check that has
        # not reported yet, else fail closed to BLOCKED.
        outcome = _merge_rejection_outcome(
            ticket.id,
            ctx.service.workspace(ticket).artifacts_dir,
            result,
            same_state=merge_fail_same_state,
        )
        if outcome.next_state is State.BLOCKED:
            self._maybe_comment(ticket, ctx, outcome.note or "")
            log.warning(
                "%s: auto-merge failed: %s — transition to BLOCKED",
                ticket.id,
                result.get("reason", "unknown"),
            )
        return outcome

    def _handle_behind_target(
        self,
        ticket: Ticket,
        ctx: StageContext,
        branch: str,
        forge: Forge,
    ) -> Outcome:
        """Try the server-side update-branch API when CI is green but the
        PR head is behind the target branch.

        On success: returns ``WAITING_AUTO_MERGE`` (CI must re-run).
        On failure within the rebase cooldown window: returns
        ``HUMAN_MR_APPROVAL``.  On failure outside the cooldown: returns
        ``IMPLEMENT_COMPLETE``.
        """
        s = ctx.settings
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
        if _within_rebase_cooldown(
            ctx.service.workspace(ticket).artifacts_dir,
            s.parked_rebase_cooldown_hours,
        ):
            log.info(
                "%s: parked PR behind-target within cooldown — staying in HUMAN_MR_APPROVAL",
                ticket.id,
            )
            return Outcome(State.HUMAN_MR_APPROVAL)
        return Outcome(
            State.IMPLEMENT_COMPLETE,
            f"branch behind target; update-branch failed: "
            f"{update_result.get('reason', 'unknown')}",
        )

    def _handle_human_mr_approval(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Poll PR status from HUMAN_MR_APPROVAL: merged/closed/conflicting/CI/auto-merge."""
        s = ctx.settings
        branch = ticket.branch or f"{s.branch_prefix}{ticket.id}"
        pr, early = self._check_pr_baseline(
            ticket, ctx, branch, State.HUMAN_MR_APPROVAL
        )
        if early is not None:
            return cast(Outcome, early)
        if pr is None:  # type guard: _check_pr_baseline guarantees pr is non-None here
            raise RuntimeError("_check_pr_baseline returned (None, None) — impossible")

        # --- Review feedback check (opt-in, gated by config flag) ---
        review_outcome = self._review_changes_requested_outcome(
            ticket,
            ctx,
            branch=branch,
            forge=get_forge(s, repo_config=ctx.repo_config),
            pr_head_sha=pr.get("sha", ""),
        )
        if review_outcome is not None:
            return cast(Outcome, review_outcome)

        # PR is open.  Check mergeability.
        mergeable = pr.get("mergeable")
        if mergeable is False:
            # PR is open and conflicting — try the server-side
            # update-branch API first (merges base into head) before
            # falling back to the heavy rebase agent.  A base-branch
            # move from a sibling PR landing often resolves cleanly;
            # only genuine content conflicts need the rebase agent.
            if self._try_update_branch_for_conflict(ticket, ctx, branch):
                return Outcome(State.HUMAN_MR_APPROVAL)
            # update-branch failed — genuine content conflict.
            if not s.autonomous_rebase_enabled:
                # Autonomous rebase disabled — fall back to the legacy
                # IMPLEMENT_COMPLETE path (rebase on next poll cycle).
                if _within_rebase_cooldown(
                    ctx.service.workspace(ticket).artifacts_dir,
                    s.parked_rebase_cooldown_hours,
                ):
                    log.info(
                        "%s: parked PR re-conflict within cooldown window — staying in HUMAN_MR_APPROVAL",
                        ticket.id,
                    )
                    return Outcome(State.HUMAN_MR_APPROVAL)
                log.info(
                    "%s: PR conflicting — falling back to IMPLEMENT_COMPLETE (autonomous rebase disabled)",
                    ticket.id,
                )
                return Outcome(
                    State.IMPLEMENT_COMPLETE,
                    "PR is now conflicting; gates no longer pass",
                )
            if _within_rebase_cooldown(
                ctx.service.workspace(ticket).artifacts_dir,
                s.parked_rebase_cooldown_hours,
            ):
                log.info(
                    "%s: parked PR re-conflict within cooldown window — staying in HUMAN_MR_APPROVAL",
                    ticket.id,
                )
                return Outcome(State.HUMAN_MR_APPROVAL)
            # Route directly to REBASING — the rebase agent will resolve
            # the conflict and the post-rebase handler will route back to
            # the merge loop (WAITING_AUTO_MERGE or HUMAN_MR_APPROVAL).
            artifacts_dir = ctx.service.workspace(ticket).artifacts_dir
            artifacts_dir.joinpath(_REBASE_FROM_STATE).write_text(
                State.HUMAN_MR_APPROVAL.value, encoding="utf-8"
            )
            log.info(
                "%s: PR conflicting — routing directly to REBASING",
                ticket.id,
            )
            return Outcome(
                State.REBASING,
                "PR is now conflicting; rebasing automatically",
            )

        # mergeable=True or None (unchecked) → no conflict. This is the
        # only true "rebase made progress" signal — clear the rebase
        # attempt counter so a *later* genuine conflict gets a fresh
        # budget (and so the counter can't accumulate across unrelated
        # conflicts).
        _write_counter(
            ctx.service.workspace(ticket).artifacts_dir / _REBASE_COUNTER,
            0,
        )

        # Check whether this repo opts out of forge-CI gating.
        from ...config.repo_settings import load_repo_skip_ci

        if load_repo_skip_ci(ctx.service.workspace(ticket).dir / "repo"):
            return Outcome(State.HUMAN_MR_APPROVAL)

        # Check remote CI before returning no-op.
        try:
            forge = get_forge(s, repo_config=ctx.repo_config)
            ci_status = forge.check_status(source_branch=branch)
        except Exception as e:
            log.warning("%s: check_status failed (retry): %s", ticket.id, e)
            return Outcome(State.HUMAN_MR_APPROVAL)

        if ci_status is None:
            # No PR or no data — standard wait.
            return Outcome(State.HUMAN_MR_APPROVAL)

        conclusion = ci_status.get("conclusion")
        if conclusion == "failure":
            log.info(
                "%s: mergeable PR has failing CI → falling back to IMPLEMENT_COMPLETE",
                ticket.id,
            )
            return Outcome(
                State.IMPLEMENT_COMPLETE, "CI is failing; gates no longer pass"
            )

        # success, pending, or None — evaluate auto-merge eligibility.
        feature_tip_sha = pr.get("sha", "")
        eligible, eligibility_reason = self._auto_merge_eligible(
            ticket, ctx, pr_head_sha=feature_tip_sha, forge=forge, pr=pr
        )

        if _ci_truly_green(conclusion, pr):
            if not eligible and self._try_skip_sensitive_path_for_approved_diff(
                ticket, ctx, forge, pr, branch, feature_tip_sha
            ):
                # Only the sensitive-path gate was blocking and a human
                # already approved an identical diff (e.g. before a
                # rebase) — skip the gate.
                eligible = True
            if eligible:
                return self._try_auto_merge(
                    ticket,
                    ctx,
                    pr,
                    branch,
                    feature_tip_sha,
                    forge,
                    merge_fail_same_state=State.IMPLEMENT_COMPLETE,
                    verify_fail_state=State.HUMAN_MR_APPROVAL,
                )
            # CI green but not eligible → human approval needed.
            self._maybe_comment(ticket, ctx, eligibility_reason)
            return Outcome(State.HUMAN_MR_APPROVAL)

        if conclusion == "success" and pr.get("mergeable_state") == "behind":
            return self._handle_behind_target(ticket, ctx, branch, forge)

        # pending, None, or a premature success (mergeable_state not yet
        # "clean") — not yet safe to merge.
        if eligible:
            self._maybe_comment(ticket, ctx, "CI pending — will auto-merge when green")
            return Outcome(State.WAITING_AUTO_MERGE)

        # Not eligible + CI pending → standard human wait.
        self._maybe_comment(ticket, ctx, eligibility_reason)
        return Outcome(State.HUMAN_MR_APPROVAL)

    def _try_update_branch_for_conflict(
        self, ticket: Ticket, ctx: StageContext, branch: str
    ) -> bool:
        """Try the server-side update-branch API to resolve a conflict.

        Called when ``mergeable is False`` — i.e. the PR's base branch
        has moved and the PR head no longer merges cleanly.  The
        update-branch API (GitHub's ``PUT /repos/.../pulls/.../update-branch``)
        merges the updated base INTO the PR head server-side.  When
        there is no true content overlap this resolves instantly and
        cheaply, avoiding the heavy rebase agent.

        Returns ``True`` when ``update_branch`` succeeded (the branch
        is now current — the caller should stay in its current state
        so CI re-runs).  Returns ``False`` when it failed (genuine
        content conflict — the caller should fall through to the
        existing IMPLEMENT_COMPLETE → REBASING path).
        """
        s = ctx.settings
        forge = get_forge(s, repo_config=ctx.repo_config)
        update_result = forge.update_branch(source_branch=branch)
        if update_result.get("updated"):
            log.info(
                "%s: PR conflicting — update-branch succeeded, "
                "branch is now current; re-polling",
                ticket.id,
            )
            self._maybe_comment(
                ticket,
                ctx,
                "PR was conflicting due to base-branch changes; "
                "auto-updated via update-branch API. "
                "Waiting for CI to re-run.",
            )
            return True
        log.info(
            "%s: PR conflicting — update-branch failed (%s); "
            "falling through to full rebase path",
            ticket.id,
            update_result.get("reason", "unknown"),
        )
        return False

    def _compute_pr_diff_hash(self, forge: Forge, ticket: Ticket) -> str | None:
        """Return a content-stable hash of the PR's file set.

        Uses sorted (filename, blob SHA) pairs so the hash is identical
        before and after a rebase when the file *content* hasn't changed.
        Returns ``None`` on any error (fail-safe).
        """
        try:
            files = forge.pr_files(source_branch=ticket.branch or "")
        except Exception:
            return None
        try:
            pairs = sorted((f.get("path", ""), f.get("sha", "")) for f in files)
            digest = hashlib.sha256(
                json.dumps(pairs, sort_keys=True).encode()
            ).hexdigest()
        except Exception:
            return None
        return digest

    def _try_skip_sensitive_path_for_approved_diff(
        self,
        ticket: Ticket,
        ctx: StageContext,
        forge: Forge,
        pr: dict[str, Any],
        branch: str,
        pr_head_sha: str,
    ) -> bool:
        """Return True if the sensitive-path gate should be skipped.

        Only returns True when ALL of:
        1. The sensitive-path gate is the *only* thing blocking auto-merge
           (all other gates pass).
        2. The current PR diff matches a previously human-approved diff
           (stored hash matches), OR the PR currently has a human
           approval — in which case we store the hash for future rebases.

        This prevents the approval storm: each rebase produces a new
        commit SHA which re-triggers the sensitive-path check, but the
        underlying file diffs haven't changed.
        """
        # Fast path: check if only sensitive-path gate blocks.
        eligible_skipped, _ = self._auto_merge_eligible(
            ticket,
            ctx,
            pr_head_sha=pr_head_sha,
            forge=forge,
            pr=pr,
            skip_sensitive_path_check=True,
        )
        if not eligible_skipped:
            return False  # Something else is blocking too.

        current_hash = self._compute_pr_diff_hash(forge, ticket)
        if current_hash is None:
            return False  # Fail-safe: can't compute hash.

        artifacts = ctx.service.workspace(ticket).artifacts_dir
        hash_path = artifacts / _APPROVED_DIFF_HASH

        # Already-approved identical diff?
        try:
            if hash_path.exists():
                stored = hash_path.read_text(encoding="utf-8").strip()
                if stored == current_hash:
                    log.info(
                        "%s: sensitive-path diff unchanged since last "
                        "human approval — skipping gate",
                        ticket.id,
                    )
                    return True
        except Exception:
            log.debug(
                "%s: could not read approved diff hash sentinel",
                ticket.id,
                exc_info=True,
            )

        # Not yet stored.  Check if the PR has a current human approval.
        try:
            review = forge.pr_review_status(source_branch=branch)
        except Exception:
            return False
        if review is None or review.get("state") != "APPROVED":
            return False

        # Human approved this exact diff — store hash for future rebases.
        try:
            hash_path.parent.mkdir(parents=True, exist_ok=True)
            hash_path.write_text(current_hash, encoding="utf-8")
        except Exception:
            log.debug(
                "%s: could not write approved diff hash sentinel",
                ticket.id,
                exc_info=True,
            )
        log.info(
            "%s: human approved sensitive-path PR — stored diff hash",
            ticket.id,
        )
        return True
