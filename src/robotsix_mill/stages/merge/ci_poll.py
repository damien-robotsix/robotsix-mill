"""CIPollMixin: CI polling and auto-merge eligibility for the merge stage.

Handles the IMPLEMENT_COMPLETE, HUMAN_MR_APPROVAL, and WAITING_AUTO_MERGE
poll paths: checks PR mergeability, CI status, auto-merge eligibility, and
routes to FIXING_CI / REBASING / WAITING_AUTO_MERGE as appropriate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...config import target_branch_for
from ...core.models import Ticket
from ...core.states import State
from ...forge import Forge, get_forge
from ..base import Outcome, StageContext
from ._base import _MergeStageBase
from ._shared import (
    _REBASE_COUNTER,
    _latest_failing_workflows,
    _verify_merge_ancestor,
    _write_counter,
    log,
)


class CIPollMixin(_MergeStageBase):
    """CI polling: gate-check, mergeability, auto-merge eligibility, main-branch debt detection."""

    def _poll_implement_complete(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Poll PR status for a ticket in IMPLEMENT_COMPLETE.

        Verify two gates before promoting to HUMAN_MR_APPROVAL:
        1. CI is green.
        2. PR is mergeable (no conflict with target).

        - Both gates pass → HUMAN_MR_APPROVAL (notify human).
        - CI failing → FIXING_CI (defer CI-fix agent).
        - Conflicting → REBASING (defer rebase agent).
        - CI pending / no data → same-state IMPLEMENT_COMPLETE (re-poll).
        - PR merged while polling → DONE.
        - PR closed → BLOCKED.
        """
        from robotsix_mill.stages import merge as _facade

        s = ctx.settings
        branch = ticket.branch or f"{s.branch_prefix}{ticket.id}"

        try:
            pr = get_forge(s, repo_config=ctx.repo_config).pr_status(
                source_branch=branch
            )
        except Exception as e:  # noqa: BLE001 — transient: retry next poll
            log.warning("%s: PR status check failed (retry): %s", ticket.id, e)
            return Outcome(State.IMPLEMENT_COMPLETE)

        if pr is None:
            return Outcome(State.IMPLEMENT_COMPLETE)  # not visible yet — re-poll
        if pr.get("merged"):
            ctx.service.workspace(ticket).artifacts_dir.joinpath("merge.md").write_text(
                f"merged: {pr.get('url', '')}\n", encoding="utf-8"
            )
            self._cleanup_branch_on_done(ticket, ctx, branch)
            log.info("%s: PR merged → done", ticket.id)
            return Outcome(State.DONE, f"merged: {pr.get('url', '')}")
        if pr.get("state") == "closed":
            return Outcome(
                State.BLOCKED,
                f"PR closed without merge — resumable: {pr.get('url', '')}",
            )

        # PR is open.  Check mergeability.
        mergeable = pr.get("mergeable")
        if mergeable is False:
            log.info(
                "%s: PR conflicting in IMPLEMENT_COMPLETE → REBASING",
                ticket.id,
            )
            return Outcome(
                State.REBASING,
                "PR is conflicting; rebase agent will run next poll",
            )

        # mergeable=True or None (unchecked) → no conflict.
        # Clear rebase attempt counter — this is signal of progress.
        _write_counter(
            ctx.service.workspace(ticket).artifacts_dir / _REBASE_COUNTER,
            0,
        )

        # Check remote CI.
        try:
            ci_status = get_forge(s, repo_config=ctx.repo_config).check_status(
                source_branch=branch
            )
        except Exception as e:  # noqa: BLE001 — transient
            log.warning("%s: check_status failed (retry): %s", ticket.id, e)
            return Outcome(State.IMPLEMENT_COMPLETE)

        if ci_status is None:
            # No CI data yet — keep waiting.
            return Outcome(State.IMPLEMENT_COMPLETE)

        conclusion = ci_status.get("conclusion")
        if conclusion == "failure":
            # Pre-existing main-branch CI debt detection (gated). When EVERY
            # workflow failing on the PR head is ALSO failing on the merge
            # target, the failure was not introduced by this PR and cannot be
            # fixed by it — rebasing onto a red main can't help, so block before
            # the branch-behind-main rebase decision below.
            if s.auto_merge_main_debt_detection_enabled:
                debt = self._main_branch_ci_debt(
                    forge=get_forge(s, repo_config=ctx.repo_config),
                    pr=pr,
                    target_branch=target_branch_for(s, ctx.repo_config),
                )
                if debt:
                    names = ", ".join(sorted(debt))
                    log.warning(
                        "%s: CI failure is pre-existing main debt (%s) → BLOCKED",
                        ticket.id,
                        names,
                    )
                    return Outcome(
                        State.BLOCKED,
                        f"CI blocked by pre-existing target-branch debt: workflow(s) "
                        f"{names} are failing on the merge target too and were not "
                        f"introduced by this PR. Operator must stabilise the target "
                        f"branch's CI before this can merge.",
                    )
            # Rebase BEFORE ci_fix when the branch is behind main. A repo-wide
            # gate (ruff/mypy/lint over the whole tree) often fails on code that
            # isn't this ticket's diff — the branch was cut from an older main
            # and main has since gained the fix. ci_fix can't repair non-ticket
            # code, but a rebase onto current main can. Self-gating: after one
            # rebase the branch is no longer behind, so a still-failing CI then
            # routes to ci_fix (a genuine, ticket-owned failure). Skipped when
            # the workspace clone is gone (None) — fall straight to ci_fix.
            repo_dir = _facade._workspace_repo_dir(ctx, ticket)
            if repo_dir is not None and _facade.git_ops.branch_is_behind_main(
                Path(repo_dir), target_branch_for(s, ctx.repo_config)
            ):
                log.info(
                    "%s: CI failing on a stale base (branch behind main) → "
                    "REBASING before ci_fix",
                    ticket.id,
                )
                return Outcome(
                    State.REBASING,
                    "CI failing and branch is behind main; rebasing onto current "
                    "main before ci_fix (the failure may be pre-existing repo-wide "
                    "debt the branch lacks the fix for)",
                )
            log.info("%s: CI failing → FIXING_CI", ticket.id)
            return Outcome(State.FIXING_CI)

        if conclusion == "success":
            # Both gates passed! Promote to human review. This is the only
            # GENUINE "CI is fixed" signal (sustained green that advances the
            # ticket), so reset the ci_fix hard cycle ceiling here — not on a
            # transient green read inside ci_fix (which a flickering CI emits
            # between failing cycles and which let a runaway loop survive).
            _write_counter(
                ctx.service.workspace(ticket).artifacts_dir / "ci_fix_cycles.txt",
                0,
            )
            log.info("%s: gates passed → HUMAN_MR_APPROVAL", ticket.id)
            return Outcome(
                State.HUMAN_MR_APPROVAL,
                "CI checks green and PR is mergeable — awaiting human merge approval",
            )

        # pending or None — keep waiting.
        return Outcome(State.IMPLEMENT_COMPLETE)

    def _handle_human_mr_approval(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Poll PR status from HUMAN_MR_APPROVAL: merged/closed/conflicting/CI/auto-merge."""
        from robotsix_mill.stages import merge as _facade

        s = ctx.settings
        branch = ticket.branch or f"{s.branch_prefix}{ticket.id}"
        try:
            pr = get_forge(s, repo_config=ctx.repo_config).pr_status(
                source_branch=branch
            )
        except Exception as e:  # noqa: BLE001 — transient: retry next poll
            log.warning("%s: PR status check failed (retry): %s", ticket.id, e)
            return Outcome(State.HUMAN_MR_APPROVAL)  # no-op

        if pr is None:
            return Outcome(State.HUMAN_MR_APPROVAL)  # not visible yet — re-poll

        if pr.get("merged"):
            ctx.service.workspace(ticket).artifacts_dir.joinpath("merge.md").write_text(
                f"merged: {pr.get('url', '')}\n", encoding="utf-8"
            )
            self._cleanup_branch_on_done(ticket, ctx, branch)
            log.info("%s: PR merged → done", ticket.id)
            return Outcome(State.DONE, f"merged: {pr.get('url', '')}")
        if pr.get("state") == "closed":
            return Outcome(
                State.BLOCKED,
                f"PR closed without merge — resumable: {pr.get('url', '')}",
            )

        # --- Review feedback check (opt-in, gated by config flag) ---
        review_outcome = self._review_changes_requested_outcome(
            ticket,
            ctx,
            branch=branch,
            forge=get_forge(s, repo_config=ctx.repo_config),
        )
        if review_outcome is not None:
            return review_outcome

        # PR is open.  Check mergeability.
        mergeable = pr.get("mergeable")
        if mergeable is False:
            # PR is open and conflicting → silent fallback to
            # IMPLEMENT_COMPLETE so the robot can auto-fix (via
            # REBASING) without notifying the human.
            log.info(
                "%s: PR conflicting — falling back to IMPLEMENT_COMPLETE",
                ticket.id,
            )
            return Outcome(
                State.IMPLEMENT_COMPLETE,
                "PR is now conflicting; gates no longer pass",
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

        # Check remote CI before returning no-op.
        try:
            ci_status = get_forge(s, repo_config=ctx.repo_config).check_status(
                source_branch=branch
            )
        except Exception as e:  # noqa: BLE001 — transient
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
        eligible, eligibility_reason = self._auto_merge_eligible(ticket, ctx)

        if conclusion == "success":
            if eligible:
                # CI green + eligible → auto-merge now.
                feature_tip_sha = pr.get("sha", "")
                result = get_forge(s, repo_config=ctx.repo_config).merge_pr(
                    source_branch=branch
                )
                if result.get("merged"):
                    repo_dir = _facade._workspace_repo_dir(ctx, ticket)
                    target = target_branch_for(s, ctx.repo_config)
                    if not _verify_merge_ancestor(
                        repo_dir, feature_tip_sha, ticket.id, target
                    ):
                        log.warning(
                            "%s: auto-merge reported success but commit %s is not an "
                            "ancestor of origin/%s — falling back to HUMAN_MR_APPROVAL",
                            ticket.id,
                            feature_tip_sha[:8] if feature_tip_sha else "(none)",
                            target,
                        )
                        return Outcome(
                            State.HUMAN_MR_APPROVAL,
                            "auto-merge reported success but merge not confirmed on origin/%s"
                            % target,
                        )
                    ctx.service.workspace(ticket).artifacts_dir.joinpath(
                        "merge.md"
                    ).write_text(
                        f"auto-merged: {pr.get('url', '')}\n",
                        encoding="utf-8",
                    )
                    self._cleanup_branch_on_done(ticket, ctx, branch)
                    log.info("%s: auto-merged → done", ticket.id)
                    return Outcome(
                        State.DONE,
                        f"auto-merged: {pr.get('url', '')}",
                    )
                # Forge rejected the merge.
                reason_text = f"forge merge failed: {result.get('reason', 'unknown')}"
                self._maybe_comment(ticket, ctx, reason_text)
                log.warning(
                    "%s: auto-merge failed: %s — falling back to human",
                    ticket.id,
                    result.get("reason", "unknown"),
                )
                return Outcome(State.HUMAN_MR_APPROVAL, reason_text)
            else:
                # CI green but not eligible → human approval needed.
                self._maybe_comment(ticket, ctx, eligibility_reason)
                return Outcome(State.HUMAN_MR_APPROVAL)

        # pending or None — not yet green.
        if eligible:
            self._maybe_comment(ticket, ctx, "CI pending — will auto-merge when green")
            return Outcome(State.WAITING_AUTO_MERGE)

        # Not eligible + CI pending → standard human wait.
        self._maybe_comment(ticket, ctx, eligibility_reason)
        return Outcome(State.HUMAN_MR_APPROVAL)

    def _auto_merge_eligible(
        self, ticket: Ticket, ctx: StageContext
    ) -> tuple[bool, str]:
        """Return ``(eligible, reason)`` for auto-merge.

        *eligible* is True when ALL of the following hold:
        1. ``settings.auto_merge_enabled`` is True
        2. ``settings.review_enabled`` is True
        3. Review artifact exists at ``{workspace}/artifacts/review.md``
        4. Artifact contains the literal string ``"auto_merge_eligible: true"``

        *reason* explains the blocking condition when eligible is False.
        """
        s = ctx.settings
        if not s.auto_merge_enabled:
            return False, "auto-merge disabled in config"
        if not s.review_enabled:
            return False, "review gate disabled — human approval required"

        review_artifact = ctx.service.workspace(ticket).artifacts_dir / "review.md"
        if not review_artifact.exists():
            return False, "no review artifact — human approval required"

        review_text = review_artifact.read_text(encoding="utf-8")
        if "auto_merge_eligible: true" not in review_text:
            # Try to read the verdict line for context.
            verdict_note = ""
            comment_note = ""
            for line in review_text.splitlines():
                if line.startswith("verdict:"):
                    verdict_note = " (" + line[len("verdict:") :].strip()[:200] + ")"
                elif line.startswith("comment:"):
                    raw = line[len("comment:") :].strip()
                    if raw and raw != "(no details)":
                        comment_note = " — " + raw[:300]
            return (
                False,
                "reviewer marked not auto-merge eligible" + verdict_note + comment_note,
            )

        return True, "eligible"

    def _poll_waiting_auto_merge(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Re-poll CI for a ticket in WAITING_AUTO_MERGE.

        The ticket was already determined eligible for auto-merge; CI was
        pending. On each poll:
        - CI success → try auto-merge (DONE or HUMAN_MR_APPROVAL on forge reject)
        - CI failure → FIXING_CI
        - CI still pending → WAITING_AUTO_MERGE (same-state no-op)
        - Eligibility lost → HUMAN_MR_APPROVAL with comment
        """
        from robotsix_mill.stages import merge as _facade

        s = ctx.settings
        branch = ticket.branch or f"{s.branch_prefix}{ticket.id}"

        # First, re-check eligibility (review artifact may have changed).
        eligible, reason = self._auto_merge_eligible(ticket, ctx)
        if not eligible:
            self._maybe_comment(ticket, ctx, reason)
            return Outcome(State.HUMAN_MR_APPROVAL, reason)

        # Re-check PR status (could have become conflicting).
        try:
            pr = get_forge(s, repo_config=ctx.repo_config).pr_status(
                source_branch=branch
            )
        except Exception as e:  # noqa: BLE001 — transient
            log.warning("%s: PR status check failed (retry): %s", ticket.id, e)
            return Outcome(State.WAITING_AUTO_MERGE)

        if pr is None:
            return Outcome(State.WAITING_AUTO_MERGE)
        if pr.get("merged"):
            sha = pr.get("sha", "")
            repo_dir = _facade._workspace_repo_dir(ctx, ticket)
            target = target_branch_for(s, ctx.repo_config)
            if _verify_merge_ancestor(repo_dir, sha, ticket.id, target):
                ctx.service.workspace(ticket).artifacts_dir.joinpath(
                    "merge.md"
                ).write_text(f"merged: {pr.get('url', '')}\n", encoding="utf-8")
                self._cleanup_branch_on_done(ticket, ctx, branch)
                log.info("%s: PR merged → done", ticket.id)
                return Outcome(State.DONE, f"merged: {pr.get('url', '')}")
            log.warning(
                "%s: PR reported merged but commit %s is not an ancestor of "
                "origin/%s — falling back to IMPLEMENT_COMPLETE for investigation",
                ticket.id,
                sha[:8] if sha else "(none)",
                target,
            )
            return Outcome(
                State.IMPLEMENT_COMPLETE,
                f"PR reported merged but merge not confirmed on origin/{target}: {pr.get('url', '')}",
            )
        if pr.get("state") == "closed":
            return Outcome(
                State.BLOCKED,
                f"PR closed without merge — resumable: {pr.get('url', '')}",
            )
        mergeable = pr.get("mergeable")
        if mergeable is False:
            log.info(
                "%s: PR became conflicting while waiting for CI → IMPLEMENT_COMPLETE",
                ticket.id,
            )
            return Outcome(
                State.IMPLEMENT_COMPLETE, "PR is now conflicting; gates no longer pass"
            )

        # --- Review feedback check (opt-in): a late CHANGES_REQUESTED must
        # short-circuit to ADDRESSING_REVIEW before any auto-merge. ---
        review_outcome = self._review_changes_requested_outcome(
            ticket,
            ctx,
            branch=branch,
            forge=get_forge(s, repo_config=ctx.repo_config),
        )
        if review_outcome is not None:
            return review_outcome

        # Check CI.
        try:
            ci_status = get_forge(s, repo_config=ctx.repo_config).check_status(
                source_branch=branch
            )
        except Exception as e:  # noqa: BLE001 — transient
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

        if conclusion == "success":
            # CI is green — attempt auto-merge.
            feature_tip_sha = pr.get("sha", "")  # capture before merge
            result = get_forge(s, repo_config=ctx.repo_config).merge_pr(
                source_branch=branch
            )
            if result.get("merged"):
                repo_dir = _facade._workspace_repo_dir(ctx, ticket)
                target = target_branch_for(s, ctx.repo_config)
                if _verify_merge_ancestor(repo_dir, feature_tip_sha, ticket.id, target):
                    ctx.service.workspace(ticket).artifacts_dir.joinpath(
                        "merge.md"
                    ).write_text(
                        f"auto-merged: {pr.get('url', '')}\n",
                        encoding="utf-8",
                    )
                    self._cleanup_branch_on_done(ticket, ctx, branch)
                    log.info("%s: auto-merged → done", ticket.id)
                    return Outcome(
                        State.DONE,
                        f"auto-merged: {pr.get('url', '')}",
                    )
                log.warning(
                    "%s: auto-merge reported success but commit %s is not an "
                    "ancestor of origin/%s — falling back to IMPLEMENT_COMPLETE",
                    ticket.id,
                    feature_tip_sha[:8] if feature_tip_sha else "(none)",
                    target,
                )
                return Outcome(
                    State.IMPLEMENT_COMPLETE,
                    f"auto-merge reported success but merge not confirmed on origin/{target}: {pr.get('url', '')}",
                )
            # Forge rejected the merge.
            reason_text = f"forge merge failed: {result.get('reason', 'unknown')}"
            self._maybe_comment(ticket, ctx, reason_text)
            log.warning(
                "%s: auto-merge failed: %s — falling back to human",
                ticket.id,
                result.get("reason", "unknown"),
            )
            return Outcome(State.HUMAN_MR_APPROVAL, reason_text)

        # Pending or None — keep waiting.
        self._maybe_comment(ticket, ctx, "CI pending — will auto-merge when green")
        return Outcome(State.WAITING_AUTO_MERGE)

    def _main_branch_ci_debt(
        self, *, forge: Forge, pr: dict[str, Any] | None, target_branch: str
    ) -> set[str]:
        """Return the failing-workflow names explained by pre-existing main debt,
        or an empty set when the failure is NOT (fully) main debt. Best-effort:
        any error / missing data → empty set (never block on uncertainty)."""
        try:
            head_sha = (pr or {}).get("sha", "")
            if not head_sha:
                return set()
            pr_failing = _latest_failing_workflows(
                forge.list_workflow_runs(head_sha=head_sha)
            )
            if not pr_failing:
                return set()
            main_failing = _latest_failing_workflows(
                forge.list_workflow_runs(branch=target_branch)
            )
            # Pre-existing debt iff EVERY workflow failing on the PR is also
            # failing on main.
            if main_failing and pr_failing <= main_failing:
                return pr_failing & main_failing
            return set()
        except Exception:  # noqa: BLE001 — best-effort; fall through to normal retry
            return set()
