"""CIPollMixin: CI polling and auto-merge eligibility for the merge stage.

Handles the IMPLEMENT_COMPLETE, HUMAN_MR_APPROVAL, and WAITING_AUTO_MERGE
poll paths: checks PR mergeability, CI status, auto-merge eligibility, and
routes to FIXING_CI / REBASING / WAITING_AUTO_MERGE as appropriate.
"""

from __future__ import annotations

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
    _AUTO_FIX_CYCLES,
    _LAST_AUTO_FIX_STAGE,
    _PING_PONG_COUNT,
    _REBASE_COUNTER,
    _ci_truly_green,
    _duplicate_changelog_fragments,
    _is_pr_check_run,
    _latest_failing_workflows,
    _read_counter,
    _verify_merge_ancestor,
    _write_counter,
    log,
)


def _extract_tracked_pr_url(description: str) -> str | None:
    """Extract the tracked PR URL from a tracker ticket description.

    Looks for the line ``- URL: <url>`` written by ``_file_foreign_ticket`` /
    ``_file_orphan_ticket``.  Returns ``None`` when not found.
    """
    m = re.search(r"- URL: (https://[^\s]+)", description)
    return m.group(1) if m else None


class CIPollMixin(_MergeStageBase):
    """CI polling: gate-check, mergeability, auto-merge eligibility, main-branch debt detection."""

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
        except Exception as e:  # noqa: BLE001 — transient: retry next poll
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
                    except Exception as exc:  # noqa: BLE001 — transient
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
            return None, Outcome(same_state)
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
        from ...vcs import git_ops
        from robotsix_mill.stages import merge as _facade

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
        except Exception:  # noqa: BLE001 — fail safe: keep BLOCKED on any error
            return False

    def _poll_implement_complete(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Poll PR status for a ticket in IMPLEMENT_COMPLETE.

        Verify two gates before promoting to HUMAN_MR_APPROVAL:
        1. CI is green.
        2. PR is mergeable (no conflict with target).

        - Both gates pass → HUMAN_MR_APPROVAL (notify human).
        - CI failing → FIXING_CI (defer CI-fix agent).
        - Conflicting → REBASING (defer rebase agent).
        - CI green but branch behind target → REBASING (defer rebase agent
          to catch the branch up; a strict up-to-date policy keeps the PR
          unmergeable until then).
        - CI pending / no data → same-state IMPLEMENT_COMPLETE (re-poll).
        - PR merged while polling → DONE.
        - PR closed → BLOCKED.
        """
        s = ctx.settings
        branch = ticket.branch or f"{s.branch_prefix}{ticket.id}"
        pr, early = self._check_pr_baseline(
            ticket, ctx, branch, State.IMPLEMENT_COMPLETE
        )
        if early is not None:
            return early
        if pr is None:  # type guard: _check_pr_baseline guarantees pr is non-None here
            raise RuntimeError("_check_pr_baseline returned (None, None) — impossible")

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

        # Check whether this repo opts out of forge-CI gating.
        from ...config.repo_settings import load_repo_skip_ci

        if load_repo_skip_ci(ctx.service.workspace(ticket).dir / "repo"):
            return Outcome(
                State.HUMAN_MR_APPROVAL,
                "CI gate skipped for this repo (skip_ci); PR mergeable — awaiting human merge approval",
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

            # --- Guardrail 1: cross-stage auto-fix cycle counter ---
            # Count every dispatch to REBASING or FIXING_CI without CI turning
            # green.  This is the universal backstop — it bounds the combined
            # rebase+ci_fix loop regardless of the alternation pattern.
            artifacts_dir = ctx.service.workspace(ticket).artifacts_dir
            auto_fix_path = artifacts_dir / _AUTO_FIX_CYCLES
            auto_fix_cycles = _read_counter(auto_fix_path)
            if s.auto_fix_max_cycles > 0 and auto_fix_cycles >= s.auto_fix_max_cycles:
                _write_counter(auto_fix_path, 0)  # reset for resume
                log.warning(
                    "%s: auto-fix exhausted cross-stage ceiling of %d cycle(s) "
                    "without CI turning green — escalating to BLOCKED",
                    ticket.id,
                    s.auto_fix_max_cycles,
                )
                return Outcome(
                    State.BLOCKED,
                    f"auto-fix exhausted cross-stage ceiling of "
                    f"{s.auto_fix_max_cycles} cycle(s) without CI turning "
                    f"green — manual intervention required (ticket "
                    f"{ticket.id}, counter was {auto_fix_cycles}). "
                    f"Resume-blocked to retry from human_mr_approval.",
                )
            _write_counter(auto_fix_path, auto_fix_cycles + 1)

            # Route to FIXING_CI. Branch-introduced failures (those green
            # on current main) go straight to ci_fix — rebasing cannot fix
            # a branch's own lint/type failure and just churns under a fast
            # main. Pre-existing main-branch debt is already blocked above.
            # The branch gets made current with main via the single
            # rebase-and-merge at the end of the merge stage, not on every
            # CI cycle.

            # --- Guardrail 2: ping-pong alternation detector ---
            ping_pong_result = self._check_ping_pong(
                ticket, ctx, artifacts_dir, routing_to="ci_fix"
            )
            if ping_pong_result is not None:
                return ping_pong_result

            log.info("%s: CI failing → FIXING_CI", ticket.id)
            return Outcome(State.FIXING_CI)

        if _ci_truly_green(conclusion, pr):
            # Both gates passed! Promote to human review. This is the only
            # GENUINE "CI is fixed" signal (sustained green that advances the
            # ticket), so reset the ci_fix hard cycle ceiling here — not on a
            # transient green read inside ci_fix (which a flickering CI emits
            # between failing cycles and which let a runaway loop survive).
            # Also reset the cross-stage auto-fix cycle counter and ping-pong
            # detector files — CI green is the ONLY genuine forward-progress
            # signal.
            # NOTE: _ci_truly_green requires mergeable_state in
            # (None, "clean", "unstable") on GitHub, so a premature green
            # (fast checks done, slow gate not yet started → mergeable_state
            # "blocked"/"behind"/"unknown") falls through to the re-poll
            # below instead of promoting. "unstable" is accepted because it
            # means mergeable with all required gates passed and only a
            # non-required status non-green (e.g. a cancelled duplicate).

            # --- Changelog duplicate-fragment gate ---
            repo_dir = str(ctx.service.workspace(ticket).dir / "repo")
            target = target_branch_for(s, ctx.repo_config)
            dups = _duplicate_changelog_fragments(repo_dir, target)
            if dups:
                log.warning(
                    "%s: duplicate changelog fragments %s → BLOCKED",
                    ticket.id,
                    sorted(dups),
                )
                return Outcome(
                    State.BLOCKED,
                    f"Duplicate changelog fragments detected for ticket(s): "
                    f"{', '.join(sorted(dups))}. Each ticket id must have exactly one "
                    f"changelog fragment — remove the extra fragment(s) and re-run. Resumable.",
                )

            artifacts_dir = ctx.service.workspace(ticket).artifacts_dir
            _write_counter(artifacts_dir / "ci_fix_cycles.txt", 0)
            _write_counter(artifacts_dir / _AUTO_FIX_CYCLES, 0)
            _write_counter(artifacts_dir / _PING_PONG_COUNT, 0)
            last_stage_path = artifacts_dir / _LAST_AUTO_FIX_STAGE
            try:
                last_stage_path.unlink()
            except FileNotFoundError:
                pass
            log.info("%s: gates passed → HUMAN_MR_APPROVAL", ticket.id)
            return Outcome(
                State.HUMAN_MR_APPROVAL,
                "CI checks green and PR is mergeable — awaiting human merge approval",
            )

        ms = pr.get("mergeable_state")
        if conclusion == "success" and ms == "behind":
            # Green CI on a stale head. Under a strict up-to-date branch
            # policy GitHub reports mergeable_state "behind" and nothing —
            # not CI, not the forge — will ever change it, so re-polling
            # waits forever (live: six chat PRs stranded, each auto-merge
            # pushing the survivors further behind). Route to REBASING so
            # the rebase agent catches the branch up and surfaces any
            # semantic conflict; CI reruns on the new head and the gates
            # re-verify.
            log.info(
                "%s: CI green but branch behind target → REBASING",
                ticket.id,
            )
            return Outcome(
                State.REBASING,
                "CI green but branch is behind the target; rebase agent "
                "will catch it up next poll",
            )

        # pending, None, or a premature success (conclusion success but
        # mergeable_state not yet promotable) — keep waiting. Log the precise
        # blocking reason so future stalls are diagnosable.
        pending_checks = ci_status.get("pending", [])
        pending_detail = f", pending checks: {pending_checks}" if pending_checks else ""
        log.info(
            "%s: re-polling IMPLEMENT_COMPLETE — conclusion=%s mergeable_state=%s%s",
            ticket.id,
            conclusion,
            ms,
            pending_detail,
        )
        return Outcome(State.IMPLEMENT_COMPLETE)

    def _check_ping_pong(
        self,
        ticket: Ticket,
        ctx: StageContext,
        artifacts_dir: Path,
        routing_to: str,
    ) -> Outcome | None:
        """Guardrail 2: detect REBASING ↔ FIXING_CI alternation (ping-pong).

        - When *routing_to* is ``"rebase"`` and the last stage was ``"ci_fix"``,
          increment the ping-pong counter.
        - When *routing_to* is ``"ci_fix"`` and the last stage was ``"rebase"``,
          increment the ping-pong counter.
        - If the counter reaches ``ping_pong_max_alternations``, reset both
          counter files and return a BLOCKED ``Outcome``.
        - Otherwise write the new last-stage marker and return ``None``
          (proceed normally).

        The ceiling guard (``> 0``) matches existing patterns: set to 0 to
        disable the detector entirely.
        """
        s = ctx.settings
        if s.ping_pong_max_alternations <= 0:
            return None

        last_stage_path = artifacts_dir / _LAST_AUTO_FIX_STAGE
        ping_pong_path = artifacts_dir / _PING_PONG_COUNT

        last_stage = ""
        try:
            last_stage = last_stage_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            pass

        # Determine whether this routing constitutes an alternation.
        alternation: bool = False
        if routing_to == "rebase" and last_stage == "ci_fix":
            alternation = True
        elif routing_to == "ci_fix" and last_stage == "rebase":
            alternation = True

        if alternation:
            ping_pong_count = _read_counter(ping_pong_path) + 1
            _write_counter(ping_pong_path, ping_pong_count)
            if ping_pong_count >= s.ping_pong_max_alternations:
                # Reset both files so a resume gets a clean budget.
                _write_counter(last_stage_path, 0)
                _write_counter(ping_pong_path, 0)
                log.warning(
                    "%s: ping-pong alternation count %d reached ceiling %d "
                    "— escalating to BLOCKED",
                    ticket.id,
                    ping_pong_count,
                    s.ping_pong_max_alternations,
                )
                return Outcome(
                    State.BLOCKED,
                    f"rebase↔ci_fix ping-pong detected: {ping_pong_count} "
                    f"alternation(s) with no CI green — manual intervention "
                    f"required (ticket {ticket.id}, ceiling is "
                    f"{s.ping_pong_max_alternations}). "
                    f"Resume-blocked to retry from human_mr_approval.",
                )

        # Write the current stage as the new "last" stage so the next
        # dispatch can detect the alternation.
        _write_counter(last_stage_path, 0)  # use write_counter for mkdir side-effect
        last_stage_path.write_text(routing_to, encoding="utf-8")

        return None

    def _handle_human_mr_approval(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Poll PR status from HUMAN_MR_APPROVAL: merged/closed/conflicting/CI/auto-merge."""
        from robotsix_mill.stages import merge as _facade

        # --- CHANGELOG lint (advisory, non-blocking) -------------------
        repo_dir = _facade._workspace_repo_dir(ctx, ticket)
        warnings = _facade._changelog_warnings_for_ticket(repo_dir, ticket.id)
        for w in warnings:
            log.warning(
                "%s: CHANGELOG %s: %s",
                ticket.id,
                w.get("severity", "warn"),
                w.get("message", ""),
            )

        s = ctx.settings
        branch = ticket.branch or f"{s.branch_prefix}{ticket.id}"
        pr, early = self._check_pr_baseline(
            ticket, ctx, branch, State.HUMAN_MR_APPROVAL
        )
        if early is not None:
            return early
        if pr is None:  # type guard: _check_pr_baseline guarantees pr is non-None here
            raise RuntimeError("_check_pr_baseline returned (None, None) — impossible")

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

        # Check whether this repo opts out of forge-CI gating.
        from ...config.repo_settings import load_repo_skip_ci

        if load_repo_skip_ci(ctx.service.workspace(ticket).dir / "repo"):
            return Outcome(State.HUMAN_MR_APPROVAL)

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
        feature_tip_sha = pr.get("sha", "")
        eligible, eligibility_reason = self._auto_merge_eligible(
            ticket, ctx, pr_head_sha=feature_tip_sha
        )

        if _ci_truly_green(conclusion, pr):
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

        if conclusion == "success" and pr.get("mergeable_state") == "behind":
            # Green CI on a stale head — the PR cannot merge until the
            # branch is caught up. Silent fallback to IMPLEMENT_COMPLETE
            # (mirroring the conflict fallback above) so the gate check
            # dispatches the rebase agent.
            log.info(
                "%s: CI green but branch behind target — falling back to "
                "IMPLEMENT_COMPLETE",
                ticket.id,
            )
            return Outcome(
                State.IMPLEMENT_COMPLETE,
                "branch is behind the target; gates no longer pass",
            )

        # pending, None, or a premature success (mergeable_state not yet
        # "clean") — not yet safe to merge.
        if eligible:
            self._maybe_comment(ticket, ctx, "CI pending — will auto-merge when green")
            return Outcome(State.WAITING_AUTO_MERGE)

        # Not eligible + CI pending → standard human wait.
        self._maybe_comment(ticket, ctx, eligibility_reason)
        return Outcome(State.HUMAN_MR_APPROVAL)

    def _auto_merge_eligible(
        self, ticket: Ticket, ctx: StageContext, pr_head_sha: str | None = None
    ) -> tuple[bool, str]:
        """Return ``(eligible, reason)`` for auto-merge.

        *eligible* is True when ALL of the following hold:
        1. ``settings.auto_merge_enabled`` is True
        2. ``settings.review_enabled`` is True
        3. Review artifact exists at ``{workspace}/artifacts/review.md``
        4. Artifact contains the literal string ``"auto_merge_eligible: true"``
        5. Artifact's ``head_sha`` matches *pr_head_sha* (when both are
           present); a mismatch means the review is stale — it was
           produced against a different branch tip.

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

        # --- head SHA freshness gate ---
        # When the artifact's head_sha differs from the current PR tip,
        # the review is stale — the branch has been rebased or force-
        # pushed since the review ran.  A stale verdict is never re-posted
        # or used to block auto-merge: treat as eligible regardless of
        # the old verdict, so the ticket can proceed.
        artifact_head_sha = ""
        for line in review_text.splitlines():
            if line.startswith("head_sha:"):
                artifact_head_sha = line[len("head_sha:") :].strip()
                break
        if pr_head_sha and artifact_head_sha and pr_head_sha != artifact_head_sha:
            return True, (
                "stale review verdict — branch head changed since last"
                " review; treating as eligible"
            )

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
        - CI green but branch behind target → IMPLEMENT_COMPLETE (the gate
          check dispatches the rebase agent to catch the branch up)
        - CI still pending → WAITING_AUTO_MERGE (same-state no-op)
        - Eligibility lost → HUMAN_MR_APPROVAL with comment
        """
        from robotsix_mill.stages import merge as _facade

        s = ctx.settings
        branch = ticket.branch or f"{s.branch_prefix}{ticket.id}"

        pr, early = self._check_pr_baseline(
            ticket, ctx, branch, State.WAITING_AUTO_MERGE, verify_merge=True
        )
        if early is not None:
            return early
        if pr is None:  # type guard: _check_pr_baseline guarantees pr is non-None here
            raise RuntimeError("_check_pr_baseline returned (None, None) — impossible")

        # Re-check eligibility (review artifact may have changed / become stale).
        feature_tip_sha = pr.get("sha", "")
        eligible, reason = self._auto_merge_eligible(
            ticket, ctx, pr_head_sha=feature_tip_sha
        )
        if not eligible:
            self._maybe_comment(ticket, ctx, reason)
            return Outcome(State.HUMAN_MR_APPROVAL, reason)

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

        if _ci_truly_green(conclusion, pr):
            # CI is green AND the forge's combined view is clean — attempt
            # auto-merge. Gating on _ci_truly_green (not bare conclusion)
            # prevents merging on a premature green: after a force-push the
            # fast checks can report success before the slow required gate
            # starts, with mergeable_state still "blocked"/"behind" — merging
            # then would redden the target branch.
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

        if conclusion == "success" and pr.get("mergeable_state") == "behind":
            # Green CI on a stale head — auto-merge can never fire under a
            # strict up-to-date policy until the branch is caught up. Fall
            # back to IMPLEMENT_COMPLETE so the gate check dispatches the
            # rebase agent (WAITING_AUTO_MERGE → REBASING is not a legal
            # transition).
            log.info(
                "%s: CI green but branch behind target while waiting for "
                "auto-merge → IMPLEMENT_COMPLETE",
                ticket.id,
            )
            return Outcome(
                State.IMPLEMENT_COMPLETE,
                "CI green but branch is behind the target; rebase needed "
                "before auto-merge",
            )

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
            pr_runs = [
                r
                for r in forge.list_workflow_runs(head_sha=head_sha)
                if _is_pr_check_run(r)
            ]
            pr_failing = _latest_failing_workflows(pr_runs)
            if not pr_failing:
                return set()
            main_runs = [
                r
                for r in forge.list_workflow_runs(branch=target_branch)
                if _is_pr_check_run(r)
            ]
            main_failing = _latest_failing_workflows(main_runs)
            # Pre-existing debt iff EVERY workflow failing on the PR is also
            # failing on main.
            if main_failing and pr_failing <= main_failing:
                return pr_failing & main_failing
            return set()
        except Exception:  # noqa: BLE001 — best-effort; fall through to normal retry
            return set()
