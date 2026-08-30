"""CI-fix stage: FIXING_CI -> IMPLEMENT_COMPLETE (fix succeeded) | BLOCKED.

When the merge stage detects a mergeable PR with failing remote CI
checks, it transitions the ticket to FIXING_CI.  This stage invokes
the ci-fix agent to auto-resolve the failures, commits locally, and
force-pushes only the ticket branch.  On success the ticket goes back
to IMPLEMENT_COMPLETE so the merge stage re-verifies both gates before
promoting to HUMAN_MR_APPROVAL.

Failure after max attempts escalates to BLOCKED (resumable).
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..agents.ci_fixing import CiFixResult, run_ci_fix_agent
from ..agents.runners.diagnostic_events import emit_diagnostic_event
from ..agents.runners.pass_runner import load_memory, persist_memory
from ..config import target_branch_for
from ..core.models import SourceKind, Ticket
from ..core.states import State
from ..forge import get_forge
from ..forge.auth import (
    _resolve_remote_url,
    github_push_token,
    github_token,
    invalidate_github_token,
)
from ..runtime import tracing
from ..vcs import git_ops
from . import dependency_fix
from .base import Outcome, Stage, StageContext
from .ci_failure_buckets import classify_ci_failure
from .ci_fix_analysis import (
    _build_failure_detail,
    _check_merge_conflict,
    _write_failing_summary_artifact,
)
from .ci_fix_codeql import (
    _CODQL_FP_TRIAGE_VERDICTS,
    _codeql_block_note,
    _partition_open_alerts,
    _try_codeql_fp_triage,
)
from .ci_fix_helpers import (
    _CI_FAILURE_FINGERPRINT,
    _CI_IDENTICAL_FAILURE_COUNT,
    _CI_REFRESH_COUNTER,
    _CODQL_CHECK_NAMES,
    _check_upstream_ci_breakage,
    _ci_failure_fingerprint,
    _detect_merge_conflict,
    _FailingContext,
    _format_alert_refs,
    _normalize_ci_failure_reason,
    _read_counter,
    _workspace_repo_dir,
    _write_counter,
    _write_text,
)

log = logging.getLogger("robotsix_mill.stages.ci_fix")


def _extract_check_names(failing_summary: str) -> str:
    """Extract a short, human-readable list of failing CI check names.

    The dispatch summary is built by :func:`_build_failing_summary` and
    uses ``## ❌ FAILED: <name>`` and ``## ✅ PASSED: <name>`` headers.
    For CodeQL-only failures the summary may start with a compact bold
    alert block (``**CodeQL alerts to fix**``).

    Returns a comma-separated string (truncated to 200 chars) or
    ``"(unknown)"`` when the summary is empty or no failing check can
    be identified.
    """
    if not failing_summary:
        return "(unknown)"
    names: list[str] = []
    for line in failing_summary.strip().splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # ``## ❌ FAILED: <name>`` — the primary check-name format.
        if stripped.startswith("## ") and "❌ FAILED:" in stripped:
            # Extract the name after the indicator.
            _, _, name = stripped.partition("❌ FAILED:")
            name = name.strip()
            if name:
                names.append(name)
            continue
        # Compact CodeQL alert block header (see ``_format_alert_summary_block``).
        # This appears at the very top when CodeQL is the only failing check.
        if stripped.startswith("**CodeQL alerts") and "**" in stripped[2:]:
            names.append("CodeQL code-scanning")
    if not names:
        return "(unknown)"
    result = ", ".join(names)
    return result[:200]


def _emit_ci_failure_event(
    ticket: Ticket,
    ctx: StageContext,
    failing: list[dict[str, Any]],
    failing_summary: str,
) -> None:
    """Emit a ``CI_FAILURE`` diagnostic event for the current failure.

    Uses the existing :func:`_normalize_ci_failure_reason` to compute a
    stable key so recurring failure modes cluster.  Deduplicates on
    ``(ticket.id, normalized_key)`` so retries of the same failure on
    the same ticket do not flood the category.

    Resolves *board_id* from ``ctx.repo_config.board_id``, falling back
    to ``ticket.board_id`` so that a missing or unresolvable repo config
    (e.g. ``_repo_config_for_ticket`` returning ``None``) never silently
    skips the emission.
    """
    try:
        board_id = (
            ctx.repo_config.board_id
            if ctx.repo_config and ctx.repo_config.board_id
            else (ticket.board_id or "")
        )
        if not board_id:
            log.warning(
                "%s: no board_id available (repo_config=%s, ticket.board_id=%s) — "
                "emitting CI_FAILURE event with empty board_id",
                ticket.id,
                ctx.repo_config.board_id if ctx.repo_config else None,
                ticket.board_id,
            )
            # Still emit — an event with an empty board_id is better than
            # a silent drop that starves the recurring-category pipeline.
            board_id = ""
        normalized_key = _normalize_ci_failure_reason(failing, failing_summary)
        names = [chk.get("name", "?") for chk in failing]
        reason = f"failing checks: {', '.join(names)}"
        klass = classify_ci_failure(failing, failing_summary)
        emitted = emit_diagnostic_event(
            ctx.settings,
            board_id,
            category="CI_FAILURE",
            ticket_id=ticket.id,
            reason=reason,
            normalized_key=normalized_key,
            bucket=klass.bucket,
            root_cause=klass.root_cause,
            prevention_rule=klass.prevention_rule,
        )
        if emitted:
            log.info(
                "%s: emitted CI_FAILURE event (board_id=%s, key=%s, bucket=%s)",
                ticket.id,
                board_id,
                normalized_key,
                klass.bucket,
            )
        else:
            log.warning(
                "%s: CI_FAILURE event NOT emitted (duplicate or write failure, "
                "board_id=%s, key=%s)",
                ticket.id,
                board_id,
                normalized_key,
            )
    except Exception:
        log.exception("%s: failed to emit CI_FAILURE event", ticket.id)


def _emit_ci_fix_resolved_event(
    ticket: Ticket,
    ctx: StageContext,
    failing: list[dict[str, Any]],
    failing_summary: str,
    result: CiFixResult,
) -> None:
    """Emit a ``CI_FIX_RESOLVED`` event when the ci-fix agent turned CI green.

    Same bucket / key as the matching ``CI_FAILURE`` event so the
    ``ci_prevention_rules`` pass can pair them; the agent's own summary of
    what it changed becomes the ``root_cause`` (it is far more specific
    than the deterministic log pick) and its ``pattern_approach``, when
    given, replaces the default prevention rule.
    """
    try:
        board_id = (
            ctx.repo_config.board_id
            if ctx.repo_config and ctx.repo_config.board_id
            else (ticket.board_id or "")
        )
        klass = classify_ci_failure(failing, failing_summary)
        names = [chk.get("name", "?") for chk in failing]
        emit_diagnostic_event(
            ctx.settings,
            board_id,
            category="CI_FIX_RESOLVED",
            ticket_id=ticket.id,
            reason=f"fixed checks: {', '.join(names)}",
            normalized_key=_normalize_ci_failure_reason(failing, failing_summary),
            bucket=klass.bucket,
            root_cause=(result.summary or klass.root_cause)[:500],
            prevention_rule=(result.pattern_approach or klass.prevention_rule)[:500],
        )
    except Exception:
        log.exception("%s: failed to emit CI_FIX_RESOLVED event", ticket.id)


class CIFixStage(Stage):
    """Check forge CI status and run automated fix logic to resolve CI failures on the ticket branch."""

    name = "ci_fix"
    input_state = State.FIXING_CI
    traced = False

    def __init__(self) -> None:
        super().__init__()
        self._last_agent_timed_out = False
        self._last_agent_timeout_elapsed: float = 0.0

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Process a FIXING_CI ticket: poll forge CI status on the ticket branch and, on failure, run the automated CI-fix agent to push corrective commits."""
        # Clone phase: guards, branch resolution, and CI status routing.
        resolved = self._resolve_clone_and_status(ticket, ctx)
        if isinstance(resolved, Outcome):
            return resolved
        (
            repo_dir,
            branch,
            failing_summary,
            failing,
            alerts,
            changed_paths,
            alerts_unreadable,
            head_sha,
            _failing_run_ids,
            _failing_run_urls,
        ) = resolved

        # --- Emit CI_FAILURE diagnostic event ---
        # Every time the stage confirms CI is genuinely failing, record a
        # bucketed diagnostic event so the ci_prevention_rules pass can
        # distil recurring failure classes into implement-ledger rules.
        _emit_ci_failure_event(ticket, ctx, failing, failing_summary)

        # --- Upstream CI breakage check ---
        # Before consuming any ci-fix attempts, check whether the target
        # branch's CI is also failing with the same checks.  If so, the
        # failure is pre-existing (not caused by this PR) — block
        # immediately rather than burning cycles on an unfixable failure.
        upstream_block = _check_upstream_ci_breakage(
            ticket.id,
            ctx.settings,
            ctx.repo_config,
            repo_dir,
            failing,
            ticket_source=ticket.source,
        )
        if upstream_block is not None:
            return Outcome(State.BLOCKED, upstream_block)

        # --- Early guard: CodeQL failing but alerts unreadable (403) ---
        # When CodeQL is among the failing checks and the alerts API
        # returned 403 (permission gap), block immediately with an
        # actionable note — the ci-fix agent must never reach the
        # blind-suppression path when alert details are unavailable.
        if alerts_unreadable and any(
            any(
                token in (chk.get("name") or "").lower() for token in _CODQL_CHECK_NAMES
            )
            for chk in failing
        ):
            codeql_note = _codeql_block_note(
                failing, alerts, changed_paths, alerts_unreadable=True
            )
            return Outcome(State.BLOCKED, codeql_note or "")

        # --- CodeQL FP triage: early trigger before consuming attempts ---
        # If CodeQL is the sole remaining red check, try FP triage
        # immediately.  The triage call has its own guardrails
        # (feature flag, run-once sentinel, eligible alerts, etc.) and
        # returns None when not applicable.
        triage_outcome = _try_codeql_fp_triage(
            ticket, ctx, failing, alerts, changed_paths
        )
        if triage_outcome is not None:
            return triage_outcome

        # Identical-failure gate: when the same CI failure fingerprint repeats
        # ci_fix_max_identical_failures times in a row, escalate to BLOCKED.
        identical_outcome = self._check_consecutive_identical_failure(
            ticket, ctx, failing_summary, head_sha
        )
        if identical_outcome is not None:
            return identical_outcome

        # --- Conflicting-PR backstop (before LLM agent) ---
        # A PR that conflicts with its target gets ZERO check runs from the
        # forge: GitHub cannot build a merge commit, so no `pull_request`
        # workflow fires. The ci-fix agent would then fix, push, and wait on
        # CI that is never going to report, spending its whole iteration
        # budget before the stage hard-blocks with "could not turn CI green
        # within its iteration budget" — a note that points at the tests
        # instead of at the conflict. Route to REBASING so the branch is
        # caught up first; CI resumes on its own once the PR is mergeable.
        conflict_outcome = self._reroute_when_pr_conflicting(ticket, ctx, branch)
        if conflict_outcome is not None:
            return conflict_outcome

        # Agent phase: the ci-fix agent now OWNS the fix→push→verify loop —
        # it fixes, pushes, and calls wait_for_ci to re-check, iterating up to
        # ci_fix_max_iterations before giving up. There is no external
        # FIXING_CI ⇄ IMPLEMENT_COMPLETE retry loop and no per-ticket cycle
        # counter: the iteration budget lives inside the wait_for_ci tool.
        log.info(
            "%s: CI failing — running ci-fix agent (owns fix/verify loop)",
            ticket.id,
        )
        return self._run_agent_and_finalize(
            ticket,
            ctx,
            repo_dir,
            branch,
            failing_summary,
            failing,
            alerts,
            changed_paths,
            head_sha,
            failing_run_urls=_failing_run_urls,
        )

    def _resolve_clone_and_status(
        self, ticket: Ticket, ctx: StageContext
    ) -> Outcome | _FailingContext:
        """Run the guards, resolve the clone, and route on CI status.

        Returns an early ``Outcome`` for every non-failure path (guards
        failing → BLOCKED; transient/None/pending/success/unknown →
        IMPLEMENT_COMPLETE). When CI is genuinely failing, returns a
        ``_FailingContext`` carrying the data the later phases need.
        """
        s = ctx.settings

        # Guard: forge configured.
        if s.forge_kind == "none":
            return Outcome(State.BLOCKED, "forge not configured")
        try:
            github_token(s)  # surfaces a clear config error early
        except RuntimeError as e:
            return Outcome(State.BLOCKED, f"forge auth not configured: {e}")

        branch = ticket.branch or f"{s.branch_prefix}{ticket.id}"

        # Guard: workspace clone must exist.
        repo_dir = _workspace_repo_dir(ctx, ticket)
        if repo_dir is None:
            return Outcome(
                State.BLOCKED,
                "workspace clone is missing; cannot fix CI. "
                "Re-run implement to recreate the clone.",
            )

        # --- Rebase onto current main before scanning CI ---
        # A stale branch can carry a CI fingerprint from an already-fixed
        # upstream issue (e.g. a resolved PYSEC advisory).  Rebase onto
        # current main and force-push so the CI re-runs against the
        # latest base — the fresh run produces a different head SHA,
        # which feeds into the failure fingerprint and prevents the
        # consecutive-identical backstop from re-blocking a ticket whose
        # upstream issue has already been resolved.
        _target = target_branch_for(s, ctx.repo_config)
        _remote_url = _resolve_remote_url(s, ctx.repo_config)
        _token = github_push_token(s, repo_config=ctx.repo_config)
        try:
            _did_rebase = git_ops.try_rebase_onto(
                Path(repo_dir),
                _target,
                remote_url=_remote_url,
                token=_token,
            )
            if _did_rebase:
                git_ops.push(Path(repo_dir), branch, _remote_url, _token)
                log.info(
                    "%s: rebased onto %s and pushed before CI scan",
                    ticket.id,
                    _target,
                )
            else:
                log.warning(
                    "%s: rebase onto %s failed or was unnecessary",
                    ticket.id,
                    _target,
                )
                # The rebase may have failed because of a merge conflict.
                # Check the forge's PR state before proceeding — a merge
                # conflict makes CI-fixing impossible and causes infinite
                # retry loops if we proceed blindly.
                conflict_block = self._check_merge_conflict(
                    ticket, ctx, repo_dir, branch, _target
                )
                if conflict_block is not None:
                    return conflict_block
                log.warning(
                    "%s: rebase failure was not a merge conflict — "
                    "proceeding with existing branch HEAD",
                    ticket.id,
                )
        except Exception:
            log.warning(
                "%s: rebase step failed — proceeding with existing branch",
                ticket.id,
                exc_info=True,
            )

        # Fetch check status from the forge.
        try:
            status = get_forge(s, repo_config=ctx.repo_config).check_status(
                source_branch=branch, require_checks=True
            )
        except Exception as e:
            log.warning("%s: check_status failed (retry): %s", ticket.id, e)
            return Outcome(State.IMPLEMENT_COMPLETE)

        if status is None:
            # PR disappeared.
            return Outcome(State.IMPLEMENT_COMPLETE)

        conclusion = status.get("conclusion")

        if conclusion == "success":
            # CI turned green while we were waiting — re-poll; merge will
            # promote to HUMAN_MR_APPROVAL.
            #
            # Do NOT reset the hard cycle ceiling here. A flickering CI (a
            # repo with several workflows / re-runs) returns a momentary
            # "success" between failing cycles; resetting on that transient
            # green was exactly what let a runaway ci_fix loop survive ~200
            # cycles (the counter never reached the ceiling). The counter is
            # reset only on GENUINE forward progress — when merge advances the
            # ticket out of the CI gate to HUMAN_MR_APPROVAL (merge.py).
            #
            # Do reset the refresh counter, though: CI going green is genuine
            # forward progress, so a later, independent staleness can be
            # refreshed once more.
            _write_counter(
                ctx.service.workspace(ticket).artifacts_dir / _CI_REFRESH_COUNTER, 0
            )
            return Outcome(State.IMPLEMENT_COMPLETE)

        if conclusion in ("pending", None):
            # Not yet complete; re-poll from human_mr_approval.
            return Outcome(State.IMPLEMENT_COMPLETE)

        if conclusion != "failure":
            # Unknown conclusion — treat as pending, re-poll.
            return Outcome(State.IMPLEMENT_COMPLETE)

        # --- CI is failing → attempt fix ---
        failing = status.get("failing", [])
        (
            failing_summary,
            alerts,
            changed_paths,
            alerts_unreadable,
            head_sha,
            failing_run_ids,
            failing_run_urls,
        ) = self._build_failure_detail(ticket, ctx, branch, failing)
        # Persist the failure detail for observability.
        self._write_failing_summary_artifact(ctx, ticket, failing_summary, failing)

        # --- Transient failure re-trigger ---
        # Before dispatching to the ci-fix agent, classify the failure.
        # Transient infrastructure flakes get automatic workflow re-runs
        # (up to ci_transient_max_retries); deterministic failures (lint,
        # tests, dead-code, type errors, etc.) proceed directly to the
        # agent for root-cause fixing.  The identical-failure gate in
        # run() bounds repeated transient re-triggers.
        from .ci_transient import is_transient_ci_failure

        _CI_TRANSIENT_RETRY_COUNTER = "ci_transient_retry.txt"

        if s.ci_transient_max_retries > 0 and is_transient_ci_failure(failing_summary):
            artifacts_dir = ctx.service.workspace(ticket).artifacts_dir
            transient_counter = _read_counter(
                artifacts_dir / _CI_TRANSIENT_RETRY_COUNTER
            )
            if transient_counter < s.ci_transient_max_retries:
                if failing_run_ids:
                    try:
                        forge = get_forge(s, repo_config=ctx.repo_config)
                        for run_id in failing_run_ids:
                            try:
                                forge.rerun_workflow(run_id=run_id)
                            except Exception:
                                log.warning(
                                    "%s: rerun_workflow failed for run %s",
                                    ticket.id,
                                    run_id,
                                    exc_info=True,
                                )
                        log.info(
                            "%s: transient CI failure — re-ran %d "
                            "workflow(s) (attempt %d/%d)",
                            ticket.id,
                            len(failing_run_ids),
                            transient_counter + 1,
                            s.ci_transient_max_retries,
                        )
                    except Exception:
                        log.warning(
                            "%s: transient re-run failed",
                            ticket.id,
                            exc_info=True,
                        )
                else:
                    log.info(
                        "%s: transient CI failure but no run IDs — re-polling",
                        ticket.id,
                    )
                _write_counter(
                    artifacts_dir / _CI_TRANSIENT_RETRY_COUNTER,
                    transient_counter + 1,
                )
                return Outcome(State.IMPLEMENT_COMPLETE)

        return _FailingContext(
            repo_dir,
            branch,
            failing_summary,
            failing,
            alerts,
            changed_paths,
            alerts_unreadable,
            head_sha,
            failing_run_ids,
            failing_run_urls,
        )

    def _check_merge_conflict(
        self,
        ticket: Ticket,
        ctx: StageContext,
        repo_dir: str,
        branch: str,
        target: str,
    ) -> Outcome | None:
        return _check_merge_conflict(
            ticket,
            ctx,
            repo_dir,
            branch,
            target,
            _detect_merge_conflict=_detect_merge_conflict,
        )

    def _build_failure_detail(
        self,
        ticket: Ticket,
        ctx: StageContext,
        branch: str,
        failing: list[dict[str, Any]],
        compact: bool = False,
    ) -> tuple[str, list[dict[str, Any]], set[str], bool, str, list[int], list[str]]:
        return _build_failure_detail(ticket, ctx, branch, failing, compact=compact)

    def _write_failing_summary_artifact(
        self,
        ctx: StageContext,
        ticket: Ticket,
        failing_summary: str,
        failing: list[dict[str, Any]],
    ) -> None:
        _write_failing_summary_artifact(ctx, ticket, failing_summary, failing)

    def _check_consecutive_identical_failure(
        self,
        ticket: Ticket,
        ctx: StageContext,
        failing_summary: str,
        head_sha: str = "",
    ) -> Outcome | None:
        """Return ``Outcome(State.BLOCKED, ...)`` when the same CI failure
        fingerprint has repeated ``ci_fix_max_identical_failures`` times in a
        row without the agent making progress, or ``None`` when the stage
        should proceed to the agent phase.

        The fingerprint is read/written from the artifacts dir.  A separate
        counter file tracks how many times the *current* fingerprint has been
        seen consecutively; it is reset to zero whenever the fingerprint
        changes (or on first run). The counter now reflects only genuine
        agent attempts on the same failure — nothing pre-seeds it before the
        agent runs.

        Short-circuits to ``None`` when ``ci_fix_max_identical_failures == 0``
        (disabled).
        """
        s = ctx.settings

        # Disabled short-circuit.
        if s.ci_fix_max_identical_failures == 0:
            return None

        repo_id = ctx.repo_config.board_id if ctx.repo_config else ""
        current_fp = _ci_failure_fingerprint(failing_summary, repo_id, head_sha)
        artifacts = ctx.service.workspace(ticket).artifacts_dir
        fp_path = artifacts / _CI_FAILURE_FINGERPRINT
        counter_path = artifacts / _CI_IDENTICAL_FAILURE_COUNT

        try:
            stored_fp = fp_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            stored_fp = ""

        if current_fp == stored_fp and stored_fp:
            # Same failure as last cycle — increment the consecutive counter.
            count = _read_counter(counter_path) + 1
            _write_counter(counter_path, count)
            if count >= s.ci_fix_max_identical_failures:
                check_names = _extract_check_names(failing_summary)
                return Outcome(
                    State.BLOCKED,
                    f"Same CI failure fingerprint ({current_fp}) repeated "
                    f"{count} consecutive times without progress on: {check_names}. "
                    "Escalating to BLOCKED — the check is deterministically "
                    "failing; resuming will not help. Fix the underlying CI issue "
                    "instead.",
                )
            return None

        # Fingerprint changed (or first run) — reset the counter and store
        # the new fingerprint for the next cycle's comparison.
        _write_counter(counter_path, 0)
        fp_path.parent.mkdir(parents=True, exist_ok=True)
        fp_path.write_text(current_fp, encoding="utf-8")
        return None

    def _run_agent_and_finalize(
        self,
        ticket: Ticket,
        ctx: StageContext,
        repo_dir: str,
        branch: str,
        failing_summary: str,
        failing: list[dict[str, Any]],
        alerts: list[dict[str, Any]],
        changed_paths: set[str],
        head_sha: str = "",
        failing_run_urls: list[str] | None = None,
    ) -> Outcome:
        """Reconcile, run the agent (which owns the loop), and route its verdict.

        The agent fixes, pushes, and verifies on real CI via wait_for_ci,
        iterating internally until CI is green (DONE) or its budget is spent
        (FAILED). There is no external retry loop here — DONE → re-poll,
        FAILED/crash → BLOCKED, OUT_OF_SCOPE → dependency fix.
        """
        s = ctx.settings

        # Reconcile with remote PR branch before running the agent so it
        # works from the latest remote state (includes any foreign commits).
        remote_url = _resolve_remote_url(s, ctx.repo_config)
        token = github_push_token(s, repo_config=ctx.repo_config)
        reconciled = git_ops.reconcile_with_remote_pr(
            Path(repo_dir), remote_url, branch, token
        )
        if reconciled is git_ops.ReconcileResult.DIVERGED:
            return Outcome(
                State.BLOCKED,
                "PR branch diverged from the workspace clone (a human likely pushed to "
                "it) — manual reconciliation required. The mill refuses to "
                "force-push here: push_with_lease cannot protect this case "
                "because reconcile's own fetch already advanced the tracking "
                "ref to the foreign commit, so a lease push would pass its "
                "compare-and-swap and SILENTLY OVERWRITE that commit.",
            )
        if reconciled is git_ops.ReconcileResult.UNAVAILABLE:
            log.warning(
                "%s: could not reach the remote PR branch to reconcile — "
                "proceeding; push_with_lease backstops a stale push",
                ticket.id,
            )

        result = self._invoke_agent(ticket, ctx, repo_dir, branch, failing_summary)

        # Write the per-cycle ci_fix.md artifact and an informative
        # history note (both best-effort) so the ticket history surfaces
        # what the agent saw and what it did.
        self._write_ci_fix_artifact(ctx, ticket, failing_summary, result)
        self._add_ci_fix_history_note(ctx, ticket, failing_summary, result)

        if result is not None and result.status == "DONE":
            _emit_ci_fix_resolved_event(ticket, ctx, failing, failing_summary, result)
            return self._finalize_success(ticket, ctx, repo_dir, branch)

        if result is not None and result.status == "OUT_OF_SCOPE":
            return self._handle_out_of_scope(
                ticket, ctx, branch, result, failing_summary, head_sha
            )

        # FAILED, or None on crash — the agent could not turn CI green within
        # its iteration budget (or hit an unrecoverable error). Block so a
        # human can intervene; resume re-enters from human_mr_approval.
        #
        # When the agent timed out (wall-clock), produce a diagnostic note
        # with the failing check(s) and elapsed time so the operator can
        # understand what CI check was being worked on, rather than a bare
        # "timed out after 2400s" from the worker.
        if result is None and self._last_agent_timed_out:
            check_names = _extract_check_names(failing_summary)
            timeout_log_url = failing_run_urls[0] if failing_run_urls else None
            url_part = f" Log: {timeout_log_url}" if timeout_log_url else ""
            timeout_note = (
                f"ci-fix agent timed out after {self._last_agent_timeout_elapsed:.0f}s "
                f"while working on: {check_names}.{url_part} "
                "The agent did not produce a result before its wall-clock budget "
                "was exhausted — it may be stuck in an analysis loop or the CI "
                "failure requires a more complex fix than the agent can apply "
                "within the time budget. Resume-blocked to retry."
            )
            recovered = self._ci_recovered_before_block(ticket, ctx, branch)
            if recovered is not None:
                return recovered
            return Outcome(State.BLOCKED, timeout_note)

        # Before emitting the generic message, check whether CodeQL code-
        # scanning is the blocker and, when it is, produce a specific note
        # naming every gating alert and explaining why the auto-solver
        # abstained.
        artifacts_dir = ctx.service.workspace(ticket).artifacts_dir
        verdicts: list[dict[str, Any]] | None = None
        try:
            import json as _json

            vp = artifacts_dir / _CODQL_FP_TRIAGE_VERDICTS
            if vp.exists():
                verdicts = _json.loads(vp.read_text(encoding="utf-8"))
        except Exception:
            pass  # best-effort verdicts read; missing/unparseable file means no verdicts

        codeql_note = _codeql_block_note(failing, alerts, changed_paths, verdicts)
        if codeql_note is not None:
            return Outcome(State.BLOCKED, codeql_note)

        check_names = _extract_check_names(failing_summary)
        budget_log_url = failing_run_urls[0] if failing_run_urls else None
        verdict_part = ""
        if result is not None and result.summary:
            verdict_part = f" Agent verdict: {result.summary}"
        url_part = f" Log: {budget_log_url}" if budget_log_url else ""
        artifact_path = ctx.service.workspace(ticket).artifacts_dir / "ci_fix.md"
        artifact_part = (
            f" See {artifact_path} for the agent's last cycle detail."
            if artifact_path.exists()
            else ""
        )
        recovered = self._ci_recovered_before_block(ticket, ctx, branch)
        if recovered is not None:
            return recovered
        return Outcome(
            State.BLOCKED,
            f"ci fix agent could not turn CI green within its iteration budget "
            f"on: {check_names}.{url_part}{verdict_part}{artifact_part} "
            "Manual intervention required — resume-blocked to retry from "
            "human_mr_approval.",
        )

    def _ci_recovered_before_block(
        self,
        ticket: Ticket,
        ctx: StageContext,
        branch: str,
    ) -> Outcome | None:
        """Re-read the current head's CI conclusion before emitting BLOCKED.

        The agent may have already pushed a fix that turned CI green (or
        that is still running) even though it exhausted its wall-clock or
        iteration budget without observing the green itself. Blocking in
        that case forces needless manual intervention, so re-probe the
        forge for the branch's current CI conclusion and, when it is
        ``success`` or ``pending``/unknown, return to the merge poll
        (``IMPLEMENT_COMPLETE``) instead of blocking.

        Returns an ``Outcome(State.IMPLEMENT_COMPLETE)`` when CI is no longer
        failing, or ``None`` when the caller should proceed to BLOCKED.
        """
        try:
            status = get_forge(ctx.settings, repo_config=ctx.repo_config).check_status(
                source_branch=branch, require_checks=True
            )
        except Exception as e:
            log.warning(
                "%s: pre-block CI re-check failed (proceeding to BLOCKED): %s",
                ticket.id,
                e,
            )
            return None

        if status is None:
            # PR disappeared — nothing to block on; re-poll.
            return Outcome(State.IMPLEMENT_COMPLETE)

        conclusion = status.get("conclusion")
        if conclusion != "failure":
            # success / pending / None / unknown — CI is not (currently)
            # failing on the head the agent last pushed. Do not block; return
            # to the merge poll so the CI wait loop re-evaluates.
            log.info(
                "%s: CI no longer failing (conclusion=%r) at block time — "
                "returning to merge poll instead of BLOCKED",
                ticket.id,
                conclusion,
            )
            return Outcome(State.IMPLEMENT_COMPLETE)

        return None

    def _write_ci_fix_artifact(
        self,
        ctx: StageContext,
        ticket: Ticket,
        failing_summary: str,
        result: CiFixResult | None,
    ) -> None:
        """Write the per-cycle ``ci_fix.md`` artifact (single latest, overwrite).

        Includes the detected failure detail and, when the agent produced a
        result, a recap of what it did and its verdict.  Best-effort only.
        """
        try:
            parts: list[str] = []
            parts.append("# CI Fix Cycle\n")
            parts.append("## Detected Failure\n")
            parts.append(failing_summary.strip() or "(no detail available)")
            parts.append("\n")
            if result is not None:
                parts.append("## Agent Recap\n")
                parts.append(f"**Verdict:** {result.status}\n")
                if result.summary:
                    parts.append(result.summary)
            else:
                parts.append("## Agent Recap\n")
                parts.append("The ci-fix agent crashed before producing a result.")
            path = ctx.service.workspace(ticket).artifacts_dir / "ci_fix.md"
            _write_text(path, "\n".join(parts))
        except Exception:
            log.exception("%s: failed to write ci_fix.md artifact", ticket.id)

    def _add_ci_fix_history_note(
        self,
        ctx: StageContext,
        ticket: Ticket,
        failing_summary: str,
        result: CiFixResult | None,
    ) -> None:
        """Record one informative history note per ci-fix cycle.

        Contains the detected failure detail and the agent's recap.
        Best-effort: a failure to write the note is logged, not raised.
        """
        try:
            lines: list[str] = []
            lines.append("**CI Fix Cycle**\n")
            lines.append("### Detected Failure\n")
            lines.append(failing_summary.strip() or "(no detail available)")
            if result is not None:
                lines.append("\n### Agent Result\n")
                lines.append(f"**Verdict:** {result.status}")
                if result.summary:
                    lines.append(result.summary)
            else:
                lines.append("\n### Agent Result\n")
                lines.append("The ci-fix agent crashed before producing a result.")
            ctx.service.add_history_note(ticket.id, "\n".join(lines))
        except Exception:
            log.exception("%s: failed to write ci-fix history note", ticket.id)

    def _invoke_agent(
        self,
        ticket: Ticket,
        ctx: StageContext,
        repo_dir: str,
        branch: str,
        failing_summary: str,
    ) -> CiFixResult | None:
        """Run the ci-fix agent inside the ticket span.

        Returns the full :class:`CiFixResult` so the caller can route on
        the agent's status (DONE / FAILED / OUT_OF_SCOPE), or ``None`` when
        the agent crashes (treated as a failure by the caller).

        The agent call is wrapped with a wall-clock timeout
        (``ci_fix_agent_timeout_seconds``) so it fails with a diagnostic
        before the worker's generic ``stage_timeout_seconds`` kills the
        entire stage silently.
        """
        s = ctx.settings
        self._last_agent_timed_out = False
        self._last_agent_timeout_elapsed = 0.0
        # Derived, not the raw field: the agent must get at least the
        # time ci_fix_max_iterations x ci_fix_wait_timeout_s promises it.
        timeout_s = s.ci_fix_agent_timeout_effective
        try:
            # ci_fix is traced=False, so wrap the LLM agent in the
            # ticket's Langfuse session (session.id = ticket.id) — same
            # reason as the rebase agent: keep its cost/traces attributed
            # to the ticket instead of an orphan root trace.
            with tracing.start_ticket_root_span(ticket.id, "ci_fix"):
                ci_fix_memory_path = s.memory_file_for(
                    "ci_fix", ctx.memory_board_id(ticket)
                )
                memory_text = load_memory(
                    ci_fix_memory_path, max_chars=s.max_memory_chars
                )

                # Pass the per-repo remote_url and token so the agent's
                # bridged git tools can drive fetch + push host-side.
                # The token provider is called at each git operation so
                # a long-running agent session always gets a fresh token.
                remote_url = _resolve_remote_url(s, ctx.repo_config)
                target = target_branch_for(s, ctx.repo_config)

                def _token_provider() -> str | None:
                    return github_push_token(s, repo_config=ctx.repo_config)

                def _token_cache_clear() -> None:
                    invalidate_github_token(s, repo_config=ctx.repo_config)

                def _run() -> CiFixResult:
                    return run_ci_fix_agent(
                        settings=s,
                        repo_dir=repo_dir,
                        branch=branch,
                        failing_summary=failing_summary,
                        memory=memory_text,
                        ticket_id=ticket.id,
                        board_id=ctx.repo_config.board_id if ctx.repo_config else "",
                        target=target,
                        remote_url=remote_url,
                        token_provider=_token_provider,
                        token_cache_clear=_token_cache_clear,
                        ci_status_fn=self._make_ci_status_fn(ticket, ctx, branch),
                        ci_log_fetch_fn=self._make_ci_log_fetch_fn(ctx, branch),
                        sandbox_image=ctx.repo_config.sandbox_image
                        if ctx.repo_config
                        else None,
                    )

                if timeout_s > 0:
                    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                    ctx_vars = contextvars.copy_context()
                    start = time.monotonic()
                    try:
                        future = executor.submit(ctx_vars.run, _run)
                        try:
                            result = future.result(timeout=timeout_s)
                        except concurrent.futures.TimeoutError:
                            future.cancel()
                            elapsed = time.monotonic() - start
                            self._last_agent_timed_out = True
                            self._last_agent_timeout_elapsed = elapsed
                            log.error(
                                "%s: ci-fix agent timed out after %.0fs "
                                "(timeout=%ds) — failing check(s): %s",
                                ticket.id,
                                elapsed,
                                timeout_s,
                                _extract_check_names(failing_summary),
                            )
                            return None
                    finally:
                        executor.shutdown(wait=False)
                else:
                    result = _run()

                if result.updated_memory:
                    persist_memory(ci_fix_memory_path, result.updated_memory)
        except Exception as e:
            log.exception("%s: ci-fix agent crashed: %s", ticket.id, e)
            return None
        return result

    def _make_ci_status_fn(
        self, ticket: Ticket, ctx: StageContext, branch: str
    ) -> Callable[[int], tuple[str, str]]:
        """Build the host-side forge probe the agent's wait_for_ci tool calls.

        Returns a closure that fetches the branch's CI conclusion and returns
        ``(conclusion, failing_summary)`` where conclusion is one of
        ``success`` / ``failure`` / ``pending`` / ``gone``. The closure takes
        the 1-based ``wait_for_ci`` attempt number: on a fresh failure it
        builds the enriched failing summary (job logs + code-scanning
        alerts) for attempt 1, and a compact digest for later attempts so
        the agent gets actionable detail without the transcript re-sending a
        full summary on every iteration. Transient forge errors map to
        ``pending`` so the agent keeps waiting rather than giving up on a
        blip.

        The closure includes a **120 s grace period** (measured from closure
        creation via ``time.monotonic()``).  During that window ANY
        ``"success"`` verdict is downgraded to ``"pending"`` so the agent
        keeps waiting — this prevents a race where GitHub's check-runs
        endpoint returns no runs (or stale runs from a prior commit) for a
        freshly-pushed SHA and the no-CI fast-path (or a stale green) would
        otherwise produce a false ``CI_PASSED``.
        """
        import time

        s = ctx.settings
        _created_at = time.monotonic()
        _grace_s = 120.0

        def status_fn(attempt: int = 1) -> tuple[str, str]:
            in_grace = (time.monotonic() - _created_at) < _grace_s

            try:
                status = get_forge(s, repo_config=ctx.repo_config).check_status(
                    source_branch=branch, require_checks=True
                )
            except Exception:
                log.warning(
                    "%s: check_status failed during CI wait — treating as pending",
                    ticket.id,
                )
                return ("pending", "")
            if status is None:
                return ("gone", "")

            conclusion = status.get("conclusion")
            sha = status.get("_sha", "")

            # During the grace period never trust "success" — check runs
            # may not be registered yet, or may be stale from a prior
            # commit.  Keep waiting.
            if conclusion == "success" and in_grace:
                return ("pending", "")

            if conclusion == "success":
                return ("success", f"CI green at {sha[:7]}" if sha else "")

            if conclusion == "failure":
                failing = status.get("failing", [])
                (
                    summary,
                    _alerts,
                    _changed,
                    _unreadable,
                    _head,
                    failing_run_ids,
                    failing_run_urls,
                ) = self._build_failure_detail(
                    ticket, ctx, branch, failing, compact=(attempt > 1)
                )
                if sha:
                    run_info_parts: list[str] = []
                    if failing_run_ids:
                        run_info_parts.append(f"run: {failing_run_ids[0]}")
                    if failing_run_urls:
                        run_info_parts.append(f"url: {failing_run_urls[0]}")
                    run_info = (
                        ", " + ", ".join(run_info_parts) if run_info_parts else ""
                    )
                    summary = f"[sha: {sha[:7]}{run_info}]\n{summary}"
                return ("failure", summary)
            # pending / None / unknown — not terminal yet.
            return ("pending", "")

        return status_fn

    def _make_ci_log_fetch_fn(
        self, ctx: StageContext, branch: str
    ) -> Callable[[int, bool], str]:
        """Build the host-side forge probe the agent's ``fetch_ci_logs`` tool calls.

        Returns a closure that calls ``forge.fetch_workflow_job_logs()`` for
        a given run id and *full_log* flag, returning the log text.  Transient
        forge errors raise through to the tool's error handler.
        """
        s = ctx.settings

        def fetch_fn(run_id: int, full_log: bool) -> str:
            forge = get_forge(s, repo_config=ctx.repo_config)
            return forge.fetch_workflow_job_logs(run_id=run_id, full_log=full_log)

        return fetch_fn

    def _reject_in_scope_alerts(
        self,
        ticket: Ticket,
        ctx: StageContext,
        in_scope_alerts: list[dict[str, Any]],
    ) -> Outcome | None:
        """Reject the out-of-scope classification when in-scope alerts exist.

        If any open code-scanning alert lives in this PR's own diff, the
        OUT_OF_SCOPE verdict is wrong for at least those — suppress the spawn
        and return IMPLEMENT_COMPLETE so the ci-fix agent re-runs against the
        in-scope-labelled failing summary.

        Returns:
            ``Outcome(State.IMPLEMENT_COMPLETE)`` when in-scope alerts are
            detected, or ``None`` when there are none (caller should proceed).
        """
        if in_scope_alerts:
            # OUT_OF_SCOPE is wrong for these alerts — suppress the spawn and
            # re-poll so the ci-fix agent re-runs against the in-scope-labelled
            # failing_summary. The agent's own wait_for_ci iteration budget
            # bounds an agent that keeps refusing, so the loop stays safe.
            try:
                ctx.service.add_history_note(
                    ticket.id,
                    "ci-fix suppressed out-of-scope spawn: the following CodeQL "
                    "alert(s) are located in THIS PR's own changed files and "
                    "must be fixed in-scope: " + _format_alert_refs(in_scope_alerts),
                )
            except Exception:
                log.warning("%s: failed to record in-scope-alert note", ticket.id)
            return Outcome(State.IMPLEMENT_COMPLETE)
        return None

    def _refresh_stale_branch_once(
        self,
        ticket: Ticket,
        ctx: StageContext,
        branch: str,
    ) -> Outcome | None:
        """Refresh the branch once if it has fallen behind its base.

        When this branch is behind its base, the failure may already be fixed
        on main (a fast-moving main races the ci-fix agent).  Refresh the
        branch once via the forge's server-side update-branch and re-poll CI
        instead of spawning a dependency fix.

        Uses the forge's server-side "behind" signal (NOT the local-clone
        ``branch_is_behind_main``, which never advances after a server-side
        refresh and would loop forever).

        Returns:
            ``Outcome(State.IMPLEMENT_COMPLETE)`` when the branch was refreshed,
            or ``None`` when no refresh is needed (caller should proceed).
        """
        s = ctx.settings
        artifacts_dir = ctx.service.workspace(ticket).artifacts_dir
        refresh_path = artifacts_dir / _CI_REFRESH_COUNTER

        # Stale-branch backstop: when this branch is behind its base, the
        # failure may already be fixed on main (a fast-moving main races the
        # ci-fix agent). Refresh the branch once via the forge's server-side
        # update-branch and re-poll CI instead of spawning a dependency fix.
        # Use the forge's server-side "behind" signal (NOT the local-clone
        # branch_is_behind_main, which never advances after a server-side
        # refresh and would loop forever).
        if _read_counter(refresh_path) == 0:
            try:
                pr = get_forge(s, repo_config=ctx.repo_config).pr_status(
                    source_branch=branch
                )
            except Exception:
                pr = None
            if (pr or {}).get("mergeable_state") == "behind":
                res = get_forge(s, repo_config=ctx.repo_config).update_branch(
                    source_branch=branch
                )
                if res.get("updated") or res.get("reason") == "already up to date":
                    _write_counter(refresh_path, 1)
                    try:
                        ctx.service.add_history_note(
                            ticket.id,
                            "branch was stale — refreshed via forge "
                            "update-branch before classifying out-of-scope; "
                            "re-running CI",
                        )
                    except Exception:
                        log.warning(
                            "%s: failed to record branch-refresh note", ticket.id
                        )
                    return Outcome(State.IMPLEMENT_COMPLETE)
                # update_branch failed (PR not found / HTTP error) — fall
                # through to the normal spawn path so we don't get stuck.
        return None

    def _reroute_when_pr_conflicting(
        self, ticket: Ticket, ctx: StageContext, branch: str
    ) -> Outcome | None:
        """Route a conflicting PR to REBASING instead of spawning the agent.

        The forge reports ``mergeable is False`` only once it has actually
        tried to build the merge commit, so this is a definite answer, not
        the ``None`` "not computed yet" state — which is left alone.

        Returns ``Outcome(State.REBASING)`` when the PR conflicts, else
        ``None`` (caller proceeds to the agent).
        """
        try:
            pr = get_forge(ctx.settings, repo_config=ctx.repo_config).pr_status(
                source_branch=branch
            )
        except Exception as e:
            # Never let a status blip divert the stage — fall through and
            # let the agent run as before.
            log.warning("%s: mergeability probe failed: %s", ticket.id, e)
            return None

        if (pr or {}).get("mergeable") is not False:
            return None

        log.info(
            "%s: PR conflicts with its target — no CI can run; "
            "routing FIXING_CI to REBASING",
            ticket.id,
        )
        return Outcome(
            State.REBASING,
            "PR conflicts with its target branch, so the forge runs no "
            "checks on it — rebasing before any CI fix is attempted.",
        )

    def _retry_transient_ci_failure(
        self,
        ticket: Ticket,
        ctx: StageContext,
        branch: str,
        failing_summary: str,
        head_sha: str,
    ) -> Outcome | None:
        """Retry CI when the failure is classified as a transient flake.

        Before spawning a blocking fix ticket, classify the failure as
        transient (infrastructure flake) vs deterministic (lint/test/type
        error).  Transient failures get automatic CI re-runs (up to
        ``ci_transient_max_retries``) instead of spawning a fix ticket.

        Returns:
            ``Outcome(State.IMPLEMENT_COMPLETE)`` when a transient re-run was
            triggered, or ``None`` when the failure is not transient, retries
            are disabled, or retries are exhausted (caller should proceed to
            spawn a fix ticket).
        """
        from .ci_transient import is_transient_ci_failure

        _CI_TRANSIENT_RETRY_COUNTER = "ci_transient_retry.txt"

        s = ctx.settings
        artifacts_dir = ctx.service.workspace(ticket).artifacts_dir

        if is_transient_ci_failure(failing_summary):
            transient_counter = _read_counter(
                artifacts_dir / _CI_TRANSIENT_RETRY_COUNTER
            )
            if transient_counter < s.ci_transient_max_retries:
                rerun_count = 0
                try:
                    forge = get_forge(s, repo_config=ctx.repo_config)
                    if head_sha:
                        runs = forge.list_workflow_runs(head_sha=head_sha)
                    else:
                        runs = []
                    for run in runs:
                        if run.get("conclusion") == "failure":
                            result_rerun = forge.rerun_workflow(run_id=run["id"])
                            if result_rerun.get("rerun"):
                                rerun_count += 1
                except Exception as exc:
                    log.warning("%s: rerun_workflow failed: %s", ticket.id, exc)

                _write_counter(
                    artifacts_dir / _CI_TRANSIENT_RETRY_COUNTER,
                    transient_counter + 1,
                )

                try:
                    ctx.service.add_history_note(
                        ticket.id,
                        f"transient CI failure detected — auto re-run "
                        f"attempt {transient_counter + 1}/"
                        f"{s.ci_transient_max_retries} "
                        f"({rerun_count} workflow(s) re-queued)",
                    )
                except Exception:
                    log.warning(
                        "%s: failed to record transient-retry note",
                        ticket.id,
                    )

                return Outcome(State.IMPLEMENT_COMPLETE)

            # Retries exhausted — fall through to spawn a fix ticket.
            try:
                ctx.service.add_history_note(
                    ticket.id,
                    f"transient CI failure persists after "
                    f"{s.ci_transient_max_retries} re-run attempts — "
                    f"escalating to fix ticket",
                )
            except Exception:
                log.warning(
                    "%s: failed to record transient-exhausted note",
                    ticket.id,
                )

        return None

    def _spawn_or_reuse_fix(
        self,
        ticket: Ticket,
        ctx: StageContext,
        branch: str,
        result: CiFixResult,
        failing_summary: str,
        head_sha: str,
        out_of_scope_alerts: list[dict[str, Any]],
    ) -> Outcome:
        """Spawn or reuse a dependency-fix ticket for the out-of-scope failure.

        Builds a deterministic title/description so the spawn is idempotent
        across cycles, delegates to
        :func:`~.dependency_fix.spawn_dependency_fix`, clears the
        ``depends_on`` relationship on the original ticket, and resets the
        per-ticket refresh counter.

        Returns:
            An ``Outcome`` from the spawn call (always returns a value —
            unlike the other helpers that return ``None`` to signal "proceed").
        """
        artifacts_dir = ctx.service.workspace(ticket).artifacts_dir
        refresh_path = artifacts_dir / _CI_REFRESH_COUNTER

        # Deterministic title so the spawn is idempotent across cycles.
        title = (
            f"ci_fix: out-of-scope CI failure — "
            f"{result.failing_check} in {result.required_change_area}"
        )
        description = (
            f"## Out-of-scope CI failure routed from {ticket.id}\n\n"
            f"**Failing check:** {result.failing_check}\n\n"
            f"**Required change area:** {result.required_change_area}\n\n"
            f"**Why out of scope:** {result.out_of_scope_reason}\n"
        )
        if out_of_scope_alerts:
            # Name the specific out-of-scope rule ids + paths so the dependency
            # fixer knows exactly which alerts to address (AC3).
            description += (
                "\n**Out-of-scope code-scanning alert(s):** "
                f"{_format_alert_refs(out_of_scope_alerts)}\n"
            )
        block_reason = "CI failure is out of scope for this ticket"

        fingerprint = _ci_failure_fingerprint(
            failing_summary,
            ctx.repo_config.board_id if ctx.repo_config else "",
            head_sha,
        )
        outcome = dependency_fix.spawn_dependency_fix(
            ticket,
            ctx,
            title=title,
            description=description,
            source_kind=SourceKind.CI_FIX_DEPENDENCY,
            block_reason_prefix=block_reason,
            priority=ticket.priority,
            dedup_labels=[f"ci_fp:{fingerprint}"],
        )

        # Clear the depends-on relationship that spawn_dependency_fix set
        # on the original ticket.  The unblocks relationship on the fix
        # ticket is sufficient — when the fix completes it auto-unblocks
        # this ticket.  Leaving depends_on set would block the operator's
        # resume-blocked: the dependency check in _process_ticket_inner
        # short-circuits before the ci_fix stage ever runs, parking the
        # ticket in FIXING_CI indefinitely.
        ctx.service.set_depends_on(ticket.id, [])

        # Reset the per-ticket refresh counter so a later re-entry (after
        # auto-unblock + a fresh pipeline pass) starts clean.
        _write_counter(refresh_path, 0)

        return outcome

    def _handle_out_of_scope(
        self,
        ticket: Ticket,
        ctx: StageContext,
        branch: str,
        result: CiFixResult,
        failing_summary: str,
        head_sha: str = "",
    ) -> Outcome:
        """Route an out-of-scope CI failure to a dedicated fix ticket.

        Before spawning, detect a *stale* branch (one behind its base, where
        the failure may already be fixed on main) and refresh it once via the
        forge's server-side update-branch primitive instead of spawning a
        dependency fix. Otherwise delegates the spawn-or-reuse + wire + park
        logic to :func:`~.dependency_fix.spawn_dependency_fix`, which is shared
        with the implement-stage baseline check (and, later, verify /
        review / merge).
        """
        # Deterministic in-diff guard: the LLM's OUT_OF_SCOPE verdict must not
        # be the only safety net. If ANY open code-scanning alert lives in this
        # PR's own diff, the verdict is wrong for at least those — do NOT spawn
        # a dependency fixer; route back to re-run the agent against the
        # in-scope-labelled summary instead.
        in_scope_alerts, out_of_scope_alerts = _partition_open_alerts(ctx, branch)

        alert_outcome = self._reject_in_scope_alerts(ticket, ctx, in_scope_alerts)
        if alert_outcome is not None:
            return alert_outcome

        refresh_outcome = self._refresh_stale_branch_once(ticket, ctx, branch)
        if refresh_outcome is not None:
            return refresh_outcome

        transient_outcome = self._retry_transient_ci_failure(
            ticket, ctx, branch, failing_summary, head_sha
        )
        if transient_outcome is not None:
            return transient_outcome

        return self._spawn_or_reuse_fix(
            ticket, ctx, branch, result, failing_summary, head_sha, out_of_scope_alerts
        )

    def _finalize_success(
        self,
        ticket: Ticket,
        ctx: StageContext,
        repo_dir: str,
        branch: str,
    ) -> Outcome:
        """On agent DONE: deterministically verify the agent's push landed
        and clobbered no foreign commits, then return to IMPLEMENT_COMPLETE so
        the merge stage re-verifies CI and promotes to HUMAN_MR_APPROVAL.

        The agent already confirmed CI green via wait_for_ci, so this is a
        cheap safety net (foreign-push / lost-push detection), not a retry
        loop. On a clean landing the refresh counter is reset so a later,
        independent staleness can rebase once more.
        """
        s = ctx.settings
        remote_url = _resolve_remote_url(s, ctx.repo_config)
        token = github_push_token(s, repo_config=ctx.repo_config)
        target = target_branch_for(s, ctx.repo_config)

        # Deterministic post-check: verify the agent's push actually
        # landed and no foreign commits were clobbered.
        check = git_ops.post_push_check(
            Path(repo_dir),
            branch=branch,
            target=target,
            remote_url=remote_url,
            token=token,
        )

        if check is git_ops.PostPushResult.PASS:
            # Genuine forward progress — allow a future staleness to refresh again.
            _write_counter(
                ctx.service.workspace(ticket).artifacts_dir / _CI_REFRESH_COUNTER, 0
            )
            log.info("%s: ci fix reported DONE, push verified", ticket.id)
            return Outcome(State.IMPLEMENT_COMPLETE)

        if check is git_ops.PostPushResult.NOT_LANDED:
            log.warning(
                "%s: ci-fix post-check failed — remote HEAD != local HEAD; "
                "push did not land",
                ticket.id,
            )
            return Outcome(
                State.BLOCKED,
                "ci fix agent reported DONE but the push did not land "
                "(remote HEAD != local HEAD). The agent may have hit a "
                "lease rejection it could not recover from. "
                "Resume-blocked to retry from human_mr_approval.",
            )

        if check is git_ops.PostPushResult.FOREIGN_DIVERGENCE:
            log.warning(
                "%s: ci-fix post-check failed — remote branch carries "
                "foreign-authored commits; a human may have pushed",
                ticket.id,
            )
            return Outcome(
                State.BLOCKED,
                "ci fix agent reported DONE but the remote branch carries "
                "foreign-authored commits — a human likely pushed to the PR "
                "branch. Manual reconciliation required. "
                "Resume-blocked to retry from human_mr_approval.",
            )

        # UNAVAILABLE — transient fetch failure, re-poll.
        log.warning(
            "%s: ci-fix post-check unavailable (fetch failed) — re-polling",
            ticket.id,
        )
        return Outcome(State.IMPLEMENT_COMPLETE)
