"""CIPollMixin: the IMPLEMENT_COMPLETE poll path for the merge stage.

Polls PR + CI status for a ticket in IMPLEMENT_COMPLETE: verifies the CI
and mergeability gates, detects pre-existing main-branch CI debt, bounds
the auto-fix / ping-pong / green-unpromotable loops, and routes to
FIXING_CI / REBASING / HUMAN_MR_APPROVAL (or merges directly when
eligible).

The shared PR preamble (``_check_pr_baseline``) and auto-merge
eligibility gate (``_auto_merge_eligible``) live on
:class:`~.pr_baseline.PrBaselineMixin`; the HUMAN_MR_APPROVAL and
WAITING_AUTO_MERGE paths live on
:class:`~.human_approval.HumanApprovalMixin` and
:class:`~.auto_merge_gate.AutoMergeGateMixin` respectively.  All resolve
at runtime through the assembled :class:`~.core.MergeStage` MRO and are
declared for the type checker on :class:`~._base._MergeStageBase`.
"""

from __future__ import annotations

import contextlib
import datetime
from pathlib import Path
from typing import Any, cast

from ...config import target_branch_for
from ...core.block_reason import TARGET_BRANCH_RED, encode
from ...core.models import SourceKind, Ticket
from ...core.states import State
from ...forge import Forge, get_forge
from ...stages.ci_transient import is_transient_ci_failure
from ..base import Outcome, StageContext
from ._base import _MergeStageBase
from ._shared import (
    _AUTO_FIX_CYCLES,
    _CI_POLL_REFRESH_SHA,
    _EMPTY_ROLLUP_COUNT,
    _EMPTY_ROLLUP_SELF_HEAL_DONE,
    _GREEN_UNPROMOTABLE_COUNT,
    _LAST_AUTO_FIX_STAGE,
    _PING_PONG_COUNT,
    _REBASE_COUNTER,
    _ci_truly_green,
    _is_pr_check_run,
    _latest_failing_workflows,
    _merge_rejection_outcome,
    _read_counter,
    _refresh_branch_for_ci,
    _verify_merge_ancestor,
    _workspace_repo_dir,
    _write_counter,
    log,
)


def _build_failing_summary(ci_status: dict[str, Any]) -> str:
    """Build a summary string from failing checks suitable for transient classification.

    Concatenates check names, output summaries, output text, and annotation
    messages from the ``failing`` list in *ci_status*.
    """
    failing: list[dict[str, Any]] = ci_status.get("failing", []) or []
    parts: list[str] = []
    for check in failing:
        name = (check.get("name") or "").strip()
        if name:
            parts.append(name)
        summary = (check.get("summary") or "").strip()
        if summary:
            parts.append(summary)
        text = (check.get("text") or "").strip()
        if text:
            parts.append(text)
        for ann in check.get("annotations", []) or []:
            msg = (ann.get("message") or "").strip()
            if msg:
                parts.append(msg)
    return "\n".join(parts)


def _ci_run_in_flight(ci_status: dict[str, Any]) -> bool:
    """True when the forge still has checks running for this branch.

    A refresh (rebase or empty commit) produces a new head SHA, which makes
    the forge start a fresh run and abandon the one in progress. Doing that
    while checks are in flight is self-defeating: the poll interval is far
    shorter than a CI run, so every poll would restart the checks it is
    waiting on and no run could ever conclude.

    Treats an absent conclusion as in-flight — a status with neither a
    conclusion nor a pending list is not evidence that anything finished,
    and declining to push is always the safe direction.
    """
    if ci_status.get("pending"):
        return True
    conclusion = ci_status.get("conclusion")
    return conclusion in (None, "", "pending", "in_progress", "queued", "waiting")


class CIPollMixin(_MergeStageBase):
    """CI polling: gate-check, mergeability, auto-merge routing, main-branch debt detection."""

    def _refresh_branch_for_ci_if_idle(
        self, ticket: Ticket, ctx: StageContext, branch: str
    ) -> None:
        """Refresh branch to force a fresh CI run before evaluating.

        Only refreshes when no CI run is currently in flight — a refresh
        (rebase or empty commit) produces a new head SHA which would make
        the forge abandon the in-progress run and start a fresh one,
        causing an endless restart cycle for runs longer than the poll
        interval.
        """
        s = ctx.settings
        _repo_dir = _workspace_repo_dir(ctx, ticket)
        _target = target_branch_for(s, ctx.repo_config)
        try:
            from ...forge.auth import _resolve_remote_url, github_push_token

            _remote_url = _resolve_remote_url(s, ctx.repo_config)
            _token = github_push_token(s, repo_config=ctx.repo_config)
        except Exception:
            _remote_url = ""
            _token = None
        # Never refresh while checks are still running. A refresh produces a
        # new head SHA, which makes the forge abandon the in-progress run and
        # start a fresh one; since the poll interval is far shorter than a CI
        # run, every poll would restart the checks it is waiting on and no run
        # could ever conclude.
        if _remote_url and _repo_dir is not None:
            try:
                _pre_status = get_forge(s, repo_config=ctx.repo_config).check_status(
                    source_branch=branch
                )
            except Exception:
                _pre_status = None
            if _pre_status is not None and _ci_run_in_flight(_pre_status):
                log.info(
                    "%s: CI still running — skipping branch refresh so the "
                    "in-flight run can finish",
                    ticket.id,
                )
            else:
                _refresh_branch_for_ci(
                    _repo_dir,
                    branch,
                    _target,
                    _remote_url,
                    _token,
                    ticket.id,
                    sentinel_path=(
                        ctx.service.workspace(ticket).artifacts_dir
                        / _CI_POLL_REFRESH_SHA
                    ),
                )

    def _handle_ci_failure_route(
        self,
        ticket: Ticket,
        ctx: StageContext,
        pr: dict[str, Any],
        branch: str,
        ci_status: dict[str, Any],
    ) -> Outcome:
        """Handle a failing CI conclusion: debt detection, guardrails, route to FIXING_CI.

        Called only when ``conclusion == "failure"``.  Checks for pre-existing
        main-branch debt, applies the cross-stage auto-fix cycle ceiling and the
        ping-pong alternation detector, then returns ``FIXING_CI`` (or ``BLOCKED``
        when a guardrail trips).
        """
        s = ctx.settings
        # Pre-existing main-branch CI debt detection (gated). When EVERY
        # workflow failing on the PR head is ALSO failing on the merge
        # target, the failure was not introduced by this PR and cannot be
        # fixed by it — rebasing onto a red main can't help, so block before
        # the branch-behind-main rebase decision below.
        # A ci_fix dependency ticket EXISTS to repair that debt, so
        # blocking it on the debt is a deadlock: the repair for red main
        # is refused because main is red, and only a human merging by
        # hand breaks the cycle.  A ``ci``-sourced ticket ("CI failure:
        # <workflow> on main") is filed by the CI monitor for exactly the
        # same reason and deadlocks the same way — robotsix-ui #56 fixed
        # main's lint and typecheck and was refused because main's lint
        # and typecheck were red, stranding two sibling tickets behind it.
        is_ci_fix = ticket.source in (
            SourceKind.CI_FIX_DEPENDENCY,
            SourceKind.CI,
        )
        if s.auto_merge_main_debt_detection_enabled and not is_ci_fix:
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
                    block_reason=encode(TARGET_BRANCH_RED, workflows=sorted(debt)),
                )

        # --- Transient infra failure: don't count against the cycle ceiling ---
        # A transient failure (runner crash, network reset, Docker flake,
        # auth outage) is not fixable by a code change — counting it against
        # the auto-fix ceiling punishes the PR for infrastructure churn.
        # When every failing check is transient, route to REBASING for a
        # fresh CI run instead of burning a cycle on FIXING_CI.
        failing_summary = _build_failing_summary(ci_status)
        all_transient = bool(failing_summary) and is_transient_ci_failure(
            failing_summary
        )
        if all_transient:
            log.info(
                "%s: CI failure is transient infra (%d failing check(s)) — "
                "skipping cycle counter, routing to REBASING for a fresh run",
                ticket.id,
                len(ci_status.get("failing", []) or []),
            )
            return Outcome(
                State.REBASING,
                "Transient CI failure detected — rebasing for a fresh run",
            )
        # --- Guardrail 1: cross-stage auto-fix cycle counter ---
        # Count every dispatch to REBASING or FIXING_CI without CI turning
        # green.  This is the universal backstop — it bounds the combined
        # rebase+ci_fix loop regardless of the alternation pattern.
        artifacts_dir = ctx.service.workspace(ticket).artifacts_dir
        auto_fix_path = artifacts_dir / _AUTO_FIX_CYCLES
        auto_fix_cycles = _read_counter(auto_fix_path)
        if s.auto_fix_max_cycles > 0 and auto_fix_cycles >= s.auto_fix_max_cycles:
            # Before hard-blocking: if the CURRENT failure is transient,
            # don't block — the ceiling was reached on infrastructure churn,
            # not a real code defect. Route to REBASING for a fresh run.
            #
            # failing_summary was already computed above for the early
            # transient gate; when we reach this point all_transient was
            # False (otherwise we'd have returned early), so this check
            # is a finer-grained "transient but not all-transient" catch.
            if failing_summary and is_transient_ci_failure(failing_summary):
                log.warning(
                    "%s: auto-fix ceiling reached (%d cycles) but current "
                    "failure is transient infra — routing to REBASING "
                    "instead of blocking",
                    ticket.id,
                    auto_fix_cycles,
                )
                return Outcome(
                    State.REBASING,
                    "Auto-fix ceiling reached but current CI failure is "
                    "transient — rebasing for a fresh run",
                )
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

    def _merge_or_promote_when_green(
        self, ticket: Ticket, ctx: StageContext, pr: dict[str, Any], branch: str
    ) -> Outcome:
        """Handle a green CI: counter reset, auto-merge or promote.

        Called only when ``_ci_truly_green(conclusion, pr)`` is True.
        Resets the ci_fix / auto-fix / ping-pong counters, attempts
        auto-merge when eligible,
        and falls back to ``HUMAN_MR_APPROVAL`` otherwise.
        """
        s = ctx.settings
        # Both gates passed! Promote to human review. This is the only
        # GENUINE "CI is fixed" signal (sustained green that advances the
        # ticket), so reset the ci_fix hard cycle ceiling here — not on a
        # transient green read inside ci_fix (which a flickering CI emits
        # between failing cycles and which let a runaway loop survive).
        # Also reset the cross-stage auto-fix cycle counter and ping-pong
        # detector files — CI green is the ONLY genuine forward-progress
        # signal.

        artifacts_dir = ctx.service.workspace(ticket).artifacts_dir
        _write_counter(artifacts_dir / "ci_fix_cycles.txt", 0)
        _write_counter(artifacts_dir / _AUTO_FIX_CYCLES, 0)
        _write_counter(artifacts_dir / _PING_PONG_COUNT, 0)
        last_stage_path = artifacts_dir / _LAST_AUTO_FIX_STAGE
        with contextlib.suppress(FileNotFoundError):
            last_stage_path.unlink()

        # Gates passed — attempt mill-native merge (not forge auto-merge).
        feature_tip_sha = pr.get("sha", "")
        eligible, eligibility_reason = self._auto_merge_eligible(
            ticket,
            ctx,
            pr_head_sha=feature_tip_sha,
            forge=get_forge(s, repo_config=ctx.repo_config),
            pr=pr,
        )
        if eligible:
            result = get_forge(s, repo_config=ctx.repo_config).merge_pr(
                source_branch=branch
            )
            if result.get("merged"):
                repo_dir = str(ctx.service.workspace(ticket).dir / "repo")
                target = target_branch_for(s, ctx.repo_config)
                if _verify_merge_ancestor(repo_dir, feature_tip_sha, ticket.id, target):
                    ctx.service.workspace(ticket).artifacts_dir.joinpath(
                        "merge.md"
                    ).write_text(
                        f"merged: {pr.get('url', '')}\n",
                        encoding="utf-8",
                    )
                    self._cleanup_branch_on_done(ticket, ctx, branch)
                    log.info("%s: merged → done", ticket.id)
                    return Outcome(
                        State.DONE,
                        f"merged: {pr.get('url', '')}",
                    )
                log.warning(
                    "%s: merge reported success but commit %s is not an "
                    "ancestor of origin/%s — falling back to IMPLEMENT_COMPLETE",
                    ticket.id,
                    feature_tip_sha[:8] if feature_tip_sha else "(none)",
                    target,
                )
                return Outcome(
                    State.IMPLEMENT_COMPLETE,
                    f"merge reported success but commit not confirmed on origin/{target}: {pr.get('url', '')}",
                )
            # Forge rejected the merge — retry a bounded number of
            # passes when it may just be a required check that has
            # not reported yet, else fail closed to BLOCKED.
            outcome = _merge_rejection_outcome(
                ticket.id,
                ctx.service.workspace(ticket).artifacts_dir,
                result,
                same_state=State.IMPLEMENT_COMPLETE,
            )
            if outcome.next_state is State.BLOCKED:
                self._maybe_comment(ticket, ctx, outcome.note or "")
                log.warning(
                    "%s: merge rejected: %s → BLOCKED",
                    ticket.id,
                    result.get("reason", "unknown"),
                )
            return outcome
        else:
            # Gates pass but not eligible for autonomous merge → ask human.
            log.info(
                "%s: gates passed but not eligible for autonomous merge → HUMAN_MR_APPROVAL",
                ticket.id,
            )
            self._maybe_comment(ticket, ctx, eligibility_reason)
            return Outcome(
                State.HUMAN_MR_APPROVAL,
                f"CI green and mergeable — {eligibility_reason}",
            )

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
            return cast(Outcome, early)
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

        self._refresh_branch_for_ci_if_idle(ticket, ctx, branch)

        # Check remote CI.
        try:
            ci_status = get_forge(s, repo_config=ctx.repo_config).check_status(
                source_branch=branch
            )
        except Exception as e:
            log.warning("%s: check_status failed (retry): %s", ticket.id, e)
            return Outcome(State.IMPLEMENT_COMPLETE)

        if ci_status is None:
            # No CI data yet — keep waiting.
            return Outcome(State.IMPLEMENT_COMPLETE)

        conclusion = ci_status.get("conclusion")

        if conclusion == "failure":
            return self._handle_ci_failure_route(ticket, ctx, pr, branch, ci_status)

        if _ci_truly_green(conclusion, pr):
            return self._merge_or_promote_when_green(ticket, ctx, pr, branch)

        ms = pr.get("mergeable_state")
        if conclusion == "success" and ms == "behind":
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

        stuck = self._check_green_unpromotable(
            ticket, ctx, pr, branch, conclusion, ms, pending_checks, ci_status
        )
        if stuck is not None:
            return stuck

        return Outcome(State.IMPLEMENT_COMPLETE)

    def _check_green_unpromotable(
        self,
        ticket: Ticket,
        ctx: StageContext,
        pr: dict[str, Any],
        branch: str,
        conclusion: str | None,
        mergeable_state: str | None,
        pending_checks: list[str],
        ci_status: dict[str, Any] | None = None,
    ) -> Outcome | None:
        """Bound the "CI green but the forge won't promote" re-poll.

        Every check the PR reports has finished and passed, yet
        ``mergeable_state`` is still un-promotable. That is normally a few
        seconds of settling — but it is *permanent* when branch protection
        requires a context no workflow on this PR produces (a renamed job,
        a workflow that no longer runs on ``pull_request``, a required
        context left over from a since-deleted check). Nothing the mill does
        can dislodge it, so re-polling only burns the worker's stage budget
        and the ticket eventually blocks with "stage merge timed out after
        Ns" — a note that names neither the PR nor the missing check.

        Also detects the *empty-rollup* case: CI reports ``"success"``
        with zero check runs and ``mergeable_state="blocked"`` — the
        signature of a PR whose ``pull_request`` event never fired.
        After a bounded number of polls the mill closes and reopens the
        PR once to trigger the event, then re-polls.  If the rollup is
        still empty afterwards, the ticket is parked with a note naming
        the close/reopen remedy already attempted.

        Returns a BLOCKED ``Outcome`` naming the unsatisfiable contexts once
        the ceiling is reached, else ``None`` (keep polling).
        """
        s = ctx.settings
        artifacts_dir = ctx.service.workspace(ticket).artifacts_dir
        counter_path = artifacts_dir / _GREEN_UNPROMOTABLE_COUNT

        # Only "everything reported is green, nothing outstanding" counts.
        # A pending check, or CI that is not success, is genuine settling.
        if conclusion != "success" or pending_checks:
            _write_counter(counter_path, 0)
            _write_counter(artifacts_dir / _EMPTY_ROLLUP_COUNT, 0)
            return None

        # --- Empty-rollup detection ---
        # CI reports "success" with zero check runs and mergeable_state
        # is "blocked": the pull_request event never fired, so no workflow
        # was triggered at the PR level.  Attempt a close/reopen self-heal.
        jobs = ci_status.get("jobs", []) if ci_status else []
        if not jobs and mergeable_state == "blocked" and s.empty_rollup_max_polls > 0:
            # --- action_required approval gate ---
            # Before assuming the pull_request event never fired (or paying
            # the close/reopen self-heal), check whether the head's workflow
            # runs are simply parked awaiting approval.  A head authored by
            # github-actions[bot] pushing with GITHUB_TOKEN (e.g. the
            # Auto-format workflow) needs workflow approval; approving the
            # runs unblocks CI with no human close/reopen needed.
            head_sha = (ci_status or {}).get("_sha") or (pr or {}).get("sha") or ""
            if head_sha:
                approved, blocked_runs = self._approve_action_required_runs(
                    ticket, ctx, head_sha
                )
                if approved:
                    return Outcome(
                        State.IMPLEMENT_COMPLETE,
                        f"Approved action_required workflow run(s) on "
                        f"{head_sha}; re-polling.",
                    )
                if blocked_runs:
                    urls = ", ".join(
                        r.get("html_url") or f"run {r.get('id')}" for r in blocked_runs
                    )
                    return Outcome(
                        State.BLOCKED,
                        f"CI is green but {pr.get('url') or branch} cannot "
                        f"be merged: {len(blocked_runs)} workflow run(s) on "
                        f"{head_sha} are parked with conclusion='action_required' "
                        f"(workflow approval required) and the approval was "
                        f"refused (403). Run URL(s): {urls}. Approve the "
                        f"workflow run(s) (or have auto-format push with the "
                        f"App token), then resume.",
                    )
            # No action_required runs (or none resolvable) — fall through to
            # the empty-rollup counter / close-reopen self-heal below.

            er_path = artifacts_dir / _EMPTY_ROLLUP_COUNT
            heal_path = artifacts_dir / _EMPTY_ROLLUP_SELF_HEAL_DONE
            er_polls = _read_counter(er_path) + 1
            _write_counter(er_path, er_polls)

            if er_polls < s.empty_rollup_max_polls:
                # Below the self-heal threshold: keep waiting.  This is a
                # *distinct* signature from green-but-unpromotable (zero check
                # runs vs. all green but the merge stalls), so it must not
                # consume the green-unpromotable budget.
                return None

            # At the threshold — the empty rollup has persisted long enough
            # to attempt the self-heal.
            if heal_path.exists():
                # Already attempted self-heal?  Park with an explicit note.
                _write_counter(er_path, 0)
                log.warning(
                    "%s: empty rollup persists after close/reopen "
                    "self-heal — parking BLOCKED",
                    ticket.id,
                )
                return Outcome(
                    State.BLOCKED,
                    f"CI is green but {pr.get('url') or branch} cannot "
                    f"be merged: the PR reports zero check runs and "
                    f"mergeable_state='blocked' — the pull_request event "
                    f"likely never fired. A close/reopen self-heal was "
                    f"already attempted without success. Manually close "
                    f"and reopen the PR (or push an empty commit) to "
                    f"trigger CI, then resume.",
                )

            # Attempt self-heal: close and reopen the PR once.  Write the
            # marker BEFORE any forge call so a later poll can never retry
            # close/reopen — the guard holds whether the calls succeed or
            # fail ("at most one self-heal per PR").
            heal_path.write_text(
                datetime.datetime.now(datetime.UTC).isoformat(),
                encoding="utf-8",
            )
            forge = get_forge(s, repo_config=ctx.repo_config)
            log.info(
                "%s: empty rollup detected (%d polls) — attempting "
                "close/reopen self-heal",
                ticket.id,
                er_polls,
            )
            closed = forge.close_pr(source_branch=branch)
            reopened = False
            if closed:
                reopened = forge.reopen_pr(source_branch=branch)
            _write_counter(er_path, 0)
            if closed and reopened:
                # Record the self-heal in ticket history so triage can
                # see that the empty rollup was detected and acted on.
                try:
                    ctx.service.add_history_note(
                        ticket.id,
                        "merge: empty CI rollup detected — closed and "
                        "reopened PR to trigger the pull_request event; "
                        "re-polling",
                    )
                except Exception:
                    log.warning(
                        "%s: failed to record empty-rollup self-heal note",
                        ticket.id,
                    )
                # Post a PR comment documenting the self-heal.
                forge.post_pr_comment(
                    source_branch=branch,
                    body=(
                        "The mill detected that no CI workflows were "
                        "triggered for this PR (empty status check rollup "
                        "with mergeable_state='blocked'). This sometimes "
                        "happens when GitHub's pull_request event is not "
                        "delivered. The PR was closed and reopened to "
                        "trigger the event and re-run CI."
                    ),
                )
                return Outcome(
                    State.IMPLEMENT_COMPLETE,
                    "Empty CI rollup detected — closed and reopened PR "
                    "to trigger pull_request event; re-polling.",
                )
            # Close or reopen failed, or only partly succeeded.  Park BLOCKED
            # immediately with an explicit note — do not fall through to the
            # green-unpromotable counter (which would burn a second budget and
            # let a later poll retry the self-heal the marker already forbids).
            stranded = (
                " The PR may have been left closed by the partial "
                "close/reopen — reopen it manually before resuming."
                if closed and not reopened
                else ""
            )
            log.warning(
                "%s: close/reopen self-heal failed (closed=%s, "
                "reopened=%s) — parking BLOCKED",
                ticket.id,
                closed,
                reopened,
            )
            return Outcome(
                State.BLOCKED,
                f"CI is green but {pr.get('url') or branch} cannot be "
                f"merged: the PR reports zero check runs and "
                f"mergeable_state='blocked' — the pull_request event "
                f"likely never fired. A close/reopen self-heal was "
                f"attempted (closed={closed}, reopened={reopened}) but "
                f"did not complete.{stranded} Close and reopen the PR "
                f"manually (or push an empty commit) to trigger CI, "
                f"then resume.",
            )

        if s.green_unpromotable_max_polls <= 0:
            return None

        polls = _read_counter(counter_path) + 1
        _write_counter(counter_path, polls)
        if polls < s.green_unpromotable_max_polls:
            return None

        _write_counter(counter_path, 0)

        target = target_branch_for(s, ctx.repo_config)
        missing = self._unsatisfiable_required_contexts(ctx, branch, target)
        if missing:
            detail = (
                f"branch protection on {target} requires the status "
                f"context(s) {sorted(missing)}, which are not among the "
                f"checks this PR reports — the job producing each was most "
                f"likely renamed or is no longer triggered. Align the check "
                f"name with the required context (or update the protection "
                f"rule), then resume."
            )
        else:
            detail = (
                f"the forge reports mergeable_state={mergeable_state!r} with "
                f"no check outstanding. Check {target}'s protection rules "
                f"against the checks this PR reports."
            )
        log.warning(
            "%s: CI green but PR unpromotable for %d consecutive polls "
            "— escalating to BLOCKED (%s)",
            ticket.id,
            polls,
            detail,
        )
        return Outcome(
            State.BLOCKED,
            f"CI is green but {pr.get('url') or branch} cannot be merged: "
            f"{detail} Resume-blocked to retry from implement_complete.",
        )

    def _approve_action_required_runs(
        self,
        ticket: Ticket,
        ctx: StageContext,
        head_sha: str,
    ) -> tuple[bool | None, list[dict[str, Any]]]:
        """Approve workflow runs on *head_sha* parked as ``action_required``.

        In the empty-rollup / ``mergeable_state="blocked"`` path, zero check
        runs can mean the head's workflows are merely waiting for approval
        rather than that the ``pull_request`` event never fired.  Lists the
        head SHA's workflow runs and approves any with
        ``conclusion == "action_required"``.

        Returns:
            ``(True, [])`` after approving at least one run;
            ``(False, [runs])`` when an approval was refused (403) — the
            runs are returned so the caller can name their URLs; and
            ``(None, [])`` when there is nothing to approve.
        """
        s = ctx.settings
        forge = get_forge(s, repo_config=ctx.repo_config)
        try:
            runs = forge.list_workflow_runs(head_sha=head_sha)
        except Exception as e:
            log.warning(
                "%s: list_workflow_runs failed for %s (retry): %s",
                ticket.id,
                head_sha,
                e,
            )
            return None, []

        pending = [r for r in runs if r.get("conclusion") == "action_required"]
        if not pending:
            return None, []

        approved = 0
        blocked_runs: list[dict[str, Any]] = []
        for run in pending:
            result = forge.approve_workflow(run_id=run["id"])
            if result.get("approved"):
                approved += 1
            elif result.get("forbidden"):
                blocked_runs.append(run)
            else:
                # Non-forbidden failure (transport / not supported) — leave
                # for a later poll rather than blocking on a true statement.
                log.warning(
                    "%s: approve workflow run %s failed (retry): %s",
                    ticket.id,
                    run["id"],
                    result.get("reason"),
                )
        if approved:
            log.info(
                "%s: approved %d action_required run(s) on %s",
                ticket.id,
                approved,
                head_sha,
            )
            return True, []
        if blocked_runs:
            return False, blocked_runs
        return None, []

    def _unsatisfiable_required_contexts(
        self, ctx: StageContext, branch: str, target: str
    ) -> set[str]:
        """Required contexts on *target* that this PR's checks cannot satisfy.

        Best-effort, and used only to word the block note — never to decide
        it. An empty result means either "nothing missing" or "protection
        could not be read". The reported set comes from ``check_status``'s
        ``jobs``, which carries check-run names and falls back to commit
        statuses only when there are no check runs at all, so on a repo using
        both a status-satisfied context can show up here spuriously.
        """
        try:
            forge = get_forge(ctx.settings, repo_config=ctx.repo_config)
            required = set(forge.required_status_contexts(target_branch=target))
            if not required:
                return set()
            status = forge.check_status(source_branch=branch) or {}
            reported = {
                name
                for name in (job.get("name") for job in status.get("jobs", []) or [])
                if isinstance(name, str)
            }
            return required - reported
        except Exception as e:  # diagnosis only — never mask the real block
            log.warning("could not diff required contexts for %s: %s", branch, e)
            return set()

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
        with contextlib.suppress(FileNotFoundError):
            last_stage = last_stage_path.read_text(encoding="utf-8").strip()

        # Determine whether this routing constitutes an alternation.
        alternation: bool = False
        if (routing_to == "rebase" and last_stage == "ci_fix") or (
            routing_to == "ci_fix" and last_stage == "rebase"
        ):
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

    def _main_branch_ci_debt(
        self, *, forge: Forge, pr: dict[str, Any] | None, target_branch: str
    ) -> set[str]:
        """Return the failing-workflow names explained by pre-existing main debt,
        or an empty set when the failure is NOT (fully) main debt. Best-effort:
        any error / missing data → empty set (never block on uncertainty).
        """
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
        except Exception:
            return set()
