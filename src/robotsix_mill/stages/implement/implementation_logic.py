"""Agent coordination, prerequisite/baseline gates, and per-pass logic.

:class:`ImplementationLogicMixin` holds the staticmethods that load the
implement context, resolve language instructions and the agent model,
invoke the coding agent, run a single fix pass, and enforce the
prerequisite and test-baseline gates.  The two largest validation
methods (scope guardrail, test-result evaluation) live in
:mod:`.validation`.  Mixed into :class:`ImplementStage` (assembled in
``phase_coordinator``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ...agents import coding, prerequisite
from ...agents.coding import AgentBudgetError, AgentRunError
from ...core.models import SourceKind, Ticket
from ...core.states import State
from ...runners.pass_runner import load_memory
from ...vcs import git_ops
from .. import dependency_fix
from ..base import Outcome, StageContext
from .file_operations import _AgentRunOutcome, _ImplementContext, _SinglePassResult

if TYPE_CHECKING:
    from .phase_coordinator import ImplementStage

log = logging.getLogger("robotsix_mill.stages.implement")


class ImplementationLogicMixin:
    """Agent-coordination / gate staticmethods mixed into :class:`ImplementStage`."""

    @staticmethod
    def _load_implement_context(
        ctx: StageContext,
        ticket: Ticket,
        settings,
    ) -> _ImplementContext:
        """Load all workspace artifacts needed before the fix loop."""
        ws = ctx.service.workspace(ticket)

        spec = ws.read_description()
        epic_ctx = ctx.service.get_epic_context(ticket)
        if epic_ctx:
            spec = epic_ctx + "\n\n" + spec

        memory_text = load_memory(
            settings.memory_file_for(
                "implement",
                ImplementStage._memory_board_id(ctx, ticket),
            ),
        )

        reference_files = None
        ref_files_path = ws.artifacts_dir / "reference_files.json"
        if ref_files_path.exists():
            reference_files = json.loads(ref_files_path.read_text(encoding="utf-8"))

        file_map: set[str] | None = None
        file_map_path = ws.artifacts_dir / "file_map.json"
        if file_map_path.exists():
            raw = json.loads(file_map_path.read_text(encoding="utf-8"))
            if raw:  # non-empty list → extract paths
                file_map = {entry["file"] for entry in raw}

        if file_map is None:
            log.warning(
                "%s: file_map.json missing or empty — skipping scope enforcement",
                ticket.id,
            )

        feedback: str | None = None
        open_thread_ids: set[int] | None = None
        # ``mill`` and ``system`` author comments (worker trace-link
        # breadcrumbs, timeout-escalation pings) are diagnostic
        # metadata, not feedback. Including them taught implement to
        # treat unreadable Langfuse URLs as review comments and ask
        # the operator "what did the reviewer say?". Trace links now
        # write to history (see worker._post_trace_event) but the
        # filter stays as defence-in-depth.
        _NON_FEEDBACK_AUTHORS = {"mill", "system"}
        if ticket.blocked_from is None:  # not a BLOCKED resume
            comments = ctx.service.list_comments(ticket.id)
            comments = [c for c in comments if c.author not in _NON_FEEDBACK_AUTHORS]
            if comments:
                open_threads = [
                    c for c in comments if c.parent_id is None and c.closed_at is None
                ]
                if open_threads:
                    open_thread_ids = {c.id for c in open_threads}
                review_feedback = "\n".join(
                    f"[REVIEW id={c.id} @ {c.created_at.isoformat()}] {c.body}"
                    for c in comments
                )
                feedback = review_feedback

        previous_attempt_summary: str | None = None
        summary_path = ws.artifacts_dir / "implement_summary.md"
        if summary_path.exists():
            try:
                previous_attempt_summary = summary_path.read_text(
                    encoding="utf-8",
                ).strip()
            except OSError:
                log.warning(
                    "%s: failed to read implement_summary.md",
                    ticket.id,
                    exc_info=True,
                )

        return _ImplementContext(
            spec=spec,
            memory_text=memory_text,
            reference_files=reference_files,
            file_map=file_map,
            feedback=feedback,
            previous_attempt_summary=previous_attempt_summary,
            open_thread_ids=open_thread_ids,
        )

    @staticmethod
    def _resolve_language_instructions(
        ctx: StageContext, ticket: Ticket, settings
    ) -> str:
        """Resolve the concatenated per-language instruction block, or
        ``""``. The repo's own ``.robotsix-mill/config.yaml`` ``languages``
        declaration (+ optional ``.robotsix-mill/language_instructions/``
        overrides) win over the central ``repos.yaml`` ``language``."""
        from ...repo_settings import resolve_language_instructions

        repo_dir = ctx.service.workspace(ticket).dir / "repo"
        return resolve_language_instructions(
            settings, repo_dir if repo_dir.exists() else None
        )

    @staticmethod
    def _select_agent_model(ic: _ImplementContext, settings) -> str | None:
        """Pick the cheaper ``no_change_model`` on a no-change-needed re-check."""
        # Gating heuristic: when the previous attempt already concluded
        # ``no_change_needed`` (the summary or feedback carries that
        # signal), the retry is a pure re-check — use the cheaper
        # ``no_change_model`` instead of the primary model.
        prev = (ic.previous_attempt_summary or "") + (ic.feedback or "")
        if "no change needed" in prev.lower():
            return settings.no_change_model
        return None

    @staticmethod
    def _invoke_implement_agent(
        ctx: StageContext,
        ticket: Ticket,
        repo_dir: Path,
        branch: str,
        settings,
        ic: _ImplementContext,
        language_instructions: str,
        agent_model: str | None,
        resume_history: list | None,
        extra_roots: list[Path] | None,
        memory_board_id: str,
    ) -> _AgentRunOutcome:
        """Invoke ``coding.run_implement_agent`` and capture caught errors.

        Returns an ``_AgentRunOutcome`` whose mutually-exclusive
        ``success`` / ``failure`` fields let the orchestrator early-return
        cleanly on budget / agent-error paths without duplicating control
        flow.  ``success`` holds the raw 7-tuple from
        ``run_implement_agent``; ``failure`` holds the
        ``_SinglePassResult`` already finalized for return.
        """
        try:
            result = coding.run_implement_agent(
                settings=settings,
                repo_dir=repo_dir,
                spec=ic.spec,
                feedback=ic.feedback,
                memory=ic.memory_text,
                reference_files=ic.reference_files,
                previous_attempt_summary=ic.previous_attempt_summary,
                file_map=ic.file_map,
                board_id=memory_board_id,
                current_ticket_id=ticket.id,
                message_history=resume_history,
                language_instructions=language_instructions,
                extra_roots=extra_roots,
                model_name=agent_model,
                sandbox_image=ctx.repo_config.sandbox_image
                if ctx.repo_config
                else None,
            )
        except AgentBudgetError as e:
            ImplementStage._finalize(
                ctx,
                ticket,
                repo_dir,
                branch,
                f"budget cap hit: {e}",
                ok=False,
                extra_roots=extra_roots,
            )
            return _AgentRunOutcome(
                failure=_SinglePassResult(
                    next_action="return",
                    outcome=Outcome(
                        State.BLOCKED,
                        f"agent budget cap — resumable (move to READY): {e}",
                    ),
                )
            )
        except AgentRunError as e:
            ImplementStage._finalize(
                ctx,
                ticket,
                repo_dir,
                branch,
                f"agent error: {e}",
                ok=False,
                extra_roots=extra_roots,
            )
            # If the original cause is a transient infra failure
            # (OpenRouter timeout, 5xx, 429), re-raise the typed cause
            # so the worker's classify_stage_error picks it up and
            # schedules a retry-with-backoff via set_retry_state.
            # Without this, every transient OpenRouter blip became a
            # hard-BLOCK that needed manual unblock (seen on ticket
            # 3106 on 2026-05-28: 4-min run, OpenRouter timeout,
            # ~hours of human attention to unstick).
            if e.cause is not None:
                from ...runtime.transient_errors import classify_stage_error

                if classify_stage_error(e.cause) == "transient":
                    raise e.cause
            return _AgentRunOutcome(
                failure=_SinglePassResult(
                    next_action="return",
                    outcome=Outcome(
                        State.BLOCKED,
                        f"agent error — resumable: {e}",
                    ),
                )
            )
        return _AgentRunOutcome(success=result)

    @staticmethod
    def _run_single_implement_pass(
        ctx: StageContext,
        ticket: Ticket,
        repo_dir: Path,
        branch: str,
        settings,
        ic: _ImplementContext,
        attempt: int,
        max_iters: int,
        resume_history: list | None,
        resuming: bool,
        extra_roots: list[Path] | None = None,
    ) -> _SinglePassResult:
        """Run one iteration of the fix loop: agent → guardrail → test gate."""
        ws = ctx.service.workspace(ticket)
        memory_board_id = ImplementStage._memory_board_id(ctx, ticket)

        language_instructions = ImplementStage._resolve_language_instructions(
            ctx,
            ticket,
            settings,
        )
        agent_model = ImplementStage._select_agent_model(ic, settings)

        agent_result = ImplementStage._invoke_implement_agent(
            ctx,
            ticket,
            repo_dir,
            branch,
            settings,
            ic,
            language_instructions,
            agent_model,
            resume_history,
            extra_roots,
            memory_board_id,
        )
        if agent_result.failure is not None:
            return agent_result.failure
        (
            summary,
            ref_files,
            updated_memory,
            conv_state,
            new_msgs,
            no_change_needed,
            no_change_rationale,
        ) = agent_result.success

        pause = ImplementStage._maybe_handle_pause(
            ctx,
            ticket,
            repo_dir,
            branch,
            ws,
            summary,
            ref_files,
            conv_state,
            new_msgs,
            extra_roots,
        )
        if pause is not None:
            return pause

        updated_ref_files, updated_prev_summary = (
            ImplementStage._persist_pass_artifacts(
                ws,
                ticket,
                ic,
                summary,
                ref_files,
                updated_memory,
                settings,
                memory_board_id,
            )
        )

        guardrail = ImplementStage._run_scope_guardrail(
            ctx,
            ticket,
            repo_dir,
            branch,
            summary,
            ref_files,
            ic.file_map,
            settings,
            ic.spec,
            ic.feedback,
        )
        if guardrail.action == "return":
            return _SinglePassResult(
                next_action="return",
                outcome=guardrail.outcome,
            )

        new_file_map = (
            guardrail.file_map if guardrail.file_map is not None else ic.file_map
        )
        new_feedback = (
            guardrail.feedback
            if guardrail.action in ("continue", "skip_iteration")
            else ic.feedback
        )
        new_ic = _ImplementContext(
            spec=ic.spec,
            memory_text=ic.memory_text,
            reference_files=updated_ref_files,
            file_map=new_file_map,
            feedback=new_feedback,
            previous_attempt_summary=updated_prev_summary,
            open_thread_ids=ic.open_thread_ids,
        )
        if guardrail.action == "continue":
            return _SinglePassResult(
                next_action="retry",
                feedback=None,
                ic=new_ic,
            )

        # guardrail.action == "skip_iteration" — fall through to test gate.
        return ImplementStage._evaluate_test_results(
            ctx,
            ticket,
            repo_dir,
            branch,
            settings,
            ic,
            new_ic,
            summary,
            ref_files,
            new_msgs,
            no_change_needed,
            no_change_rationale,
            resuming,
            attempt,
            max_iters,
            extra_roots,
        )

    @staticmethod
    def _run_prerequisite_gate(
        ctx: StageContext,
        ticket: Ticket,
        spec: str,
        repo_dir: Path,
        s,
    ) -> Outcome | None:
        """Deterministic pre-agent gate for external prerequisites.

        Verifies that symbol/import prerequisites the spec declares in a
        ````prereq```` block are satisfiable in the cloned repo's
        environment before the expensive coordinator agent runs.  This
        is the cheapest gate (regex parse + bounded subprocess), so it
        runs first.

        No-op (returns ``None``) when ``prerequisite_gate_enabled`` is
        False.  When a declared prerequisite is unmet the ticket is
        BLOCKED — the work is still required once the upstream symbol
        lands (unlike the freshness gate, which routes stale findings to
        DONE).  Best-effort: any checker error logs a warning and
        proceeds (returns ``None``) rather than blocking.
        """
        if not s.prerequisite_gate_enabled:
            return None

        try:
            result = prerequisite.run_prerequisite_check(spec, repo_dir)
        except Exception:
            log.warning(
                "%s: prerequisite check failed, proceeding with implement",
                ticket.id,
                exc_info=True,
            )
            return None

        unmet = result.get("unmet") or []
        if unmet:
            joined = ", ".join(unmet)
            log.info(
                "%s: prerequisite gate blocked — unmet: %s",
                ticket.id,
                joined,
            )
            return Outcome(
                State.BLOCKED,
                f"prerequisite(s) not met: {joined}. Re-run implement "
                "(resume-blocked) once the prerequisite is available.",
            )
        return None

    @staticmethod
    def _run_baseline_check(
        ctx: StageContext,
        ticket: Ticket,
        repo_dir: Path,
        branch: str,
        resuming: bool,
        settings,
    ) -> Outcome | None:
        """Run the test gate on the base branch BEFORE the agent loop.

        Returns ``Outcome`` to short-circuit (BLOCKED), or ``None`` to
        proceed.  The result is cached at ``artifacts/baseline_check.json``
        keyed by base-branch SHA so retries don't re-execute.
        """
        ws = ctx.service.workspace(ticket)
        cache_path = ws.artifacts_dir / "baseline_check.json"

        # Resolve the current base-branch SHA. Prefer the remote ref
        # (origin/<branch>) — the local branch may be stale, and we must test
        # the SAME commit we report as base_sha (see checkout below).
        remote_sha = git_ops.remote_branch_sha(repo_dir, settings.forge_target_branch)
        base_sha = remote_sha or git_ops.head_sha(repo_dir)

        # --- idempotency guard (per ticket, per base commit) ---
        # If a baseline-fix this ticket already depends on has completed for
        # THIS base_sha (same title), the gate is satisfied — re-running it
        # would re-spawn a duplicate fix (the prior DONE fix is invisible to
        # spawn_dependency_fix's open-only dedup), wedging the ticket in an
        # operator-only re-spawn cycle. Placed before the cache read so it
        # covers BOTH the cache-hit-failing and fresh-fail paths and avoids
        # re-running the test agent on re-entry. Proceed instead; any genuine
        # residual failure is caught downstream as a normal gate result.
        fix_title = ImplementStage._baseline_fix_title(settings, base_sha)
        resolved_fix_id = ImplementStage._baseline_fix_already_resolved(
            ctx, ticket, fix_title
        )
        if resolved_fix_id is not None:
            try:
                ctx.service.add_history_note(
                    ticket.id,
                    f"baseline gate already satisfied by completed fix "
                    f"{resolved_fix_id} for base {base_sha[:8]} — proceeding.",
                )
            except Exception:  # noqa: BLE001 — history note is best-effort
                log.warning(
                    "%s: failed to record baseline-gate-satisfied note",
                    ticket.id,
                )
            return None

        # --- cache lookup ---
        if cache_path.exists():
            try:
                cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError, OSError:
                cache = None
            if isinstance(cache, dict):
                cached_sha = cache.get("base_sha")
                cached_passed = cache.get("passed")
                if cached_sha == base_sha:
                    # Same base commit → reuse cached result.
                    if cached_passed:
                        return None
                    diag = cache.get("diagnosis", "pre-existing test failures")
                    return ImplementStage._spawn_baseline_fix(
                        ctx, ticket, diag, base_sha, settings
                    )
                if cached_passed:
                    # Base advanced but cached result was passing — a
                    # passing baseline stays valid (AC7).
                    return None
                # Base advanced AND cached result was failing → re-run
                # (operator may have fixed the branch between retries).

        # --- execute baseline check ---
        # Check out the EXACT base commit (origin/<branch>), not the local
        # branch ref — the clone's local main is often stale, which made the
        # baseline run old code while labelling it with the fresh remote SHA,
        # producing phantom "pre-existing failures on main" (e.g. a fix that
        # already landed reported as still-broken) that poison the gate. When
        # the remote branch is absent, fall back to the branch name.
        git_ops.checkout(repo_dir, remote_sha or settings.forge_target_branch)
        try:
            # retry_on_failure: one flaky test on main must not fabricate
            # "pre-existing test failures", block the ticket, and spawn a
            # bogus dependency-fix ticket — re-run once before believing it.
            from robotsix_mill.stages import implement as _impl_pkg

            passed, diag = _impl_pkg.run_test_agent(
                settings=settings,
                repo_dir=repo_dir,
                repo_config=ctx.repo_config,
                retry_on_failure=True,
            )
        finally:
            git_ops.checkout(repo_dir, branch)

        cache_data: dict[str, object] = {
            "passed": passed,
            "diagnosis": diag,
            "base_sha": base_sha,
        }
        cache_path.write_text(json.dumps(cache_data, indent=2), encoding="utf-8")

        if passed:
            return None

        # Write the implement.md artifact so the blocked ticket has a
        # matching diagnostic (AC8 / existing BLOCKED pattern).
        ImplementStage._finalize(
            ctx,
            ticket,
            repo_dir,
            branch,
            f"pre-existing test failures on {settings.forge_target_branch} "
            f"({base_sha[:8]}): {diag[:400]}",
            ok=False,
            extra_roots=None,
        )
        return ImplementStage._spawn_baseline_fix(ctx, ticket, diag, base_sha, settings)

    @staticmethod
    def _baseline_fix_title(settings, base_sha: str) -> str:
        """Deterministic title for the baseline-fix ticket of *base_sha*.

        Shared by :meth:`_run_baseline_check` (idempotency guard) and
        :meth:`_spawn_baseline_fix` (spawn/dedup) so the two cannot drift.
        """
        return (
            f"baseline: pre-existing test failures — "
            f"{settings.forge_target_branch} {base_sha[:8]}"
        )

    @staticmethod
    def _baseline_fix_already_resolved(ctx, ticket, fix_title) -> str | None:
        """Return the id of an already-completed baseline-fix this ticket
        depends on (same title => same base_sha), else None."""
        for dep_id in ctx.service._parse_depends_on(ticket):
            dep = ctx.service.get(dep_id)
            if (
                dep is not None
                and dep.source == SourceKind.IMPLEMENT_BASELINE_DEPENDENCY
                and dep.title == fix_title
                and dep.state in (State.DONE, State.CLOSED)
            ):
                return dep.id
        return None

    @staticmethod
    def _spawn_baseline_fix(
        ctx: StageContext,
        ticket: Ticket,
        diag: str,
        base_sha: str,
        settings,
    ) -> Outcome:
        """Spawn (or reuse) a fix ticket for pre-existing baseline failures.

        Uses the shared :func:`~.dependency_fix.spawn_dependency_fix`
        helper so the current ticket auto-resumes when the fix reaches
        DONE instead of dead-ending on ``BLOCKED``.
        """
        block_reason = (
            f"pre-existing test failures on {settings.forge_target_branch} "
            f"({base_sha[:8]}): {diag[:400]}"
        )
        title = ImplementStage._baseline_fix_title(settings, base_sha)
        description = (
            f"## Pre-existing test failures on {settings.forge_target_branch}\n\n"
            f"**Base SHA:** {base_sha}\n\n"
            f"**Diagnosis:** {diag}\n\n"
            f"**Detected by:** implement baseline check for {ticket.id}\n"
        )
        return dependency_fix.spawn_dependency_fix(
            ticket,
            ctx,
            title=title,
            description=description,
            source_kind=SourceKind.IMPLEMENT_BASELINE_DEPENDENCY,
            block_reason_prefix=block_reason,
            priority=ticket.priority,
        )

    @staticmethod
    def _memory_board_id(ctx: StageContext, ticket: Ticket) -> str:
        """Resolve the board_id used to key the implement memory ledger.

        Meta-board tickets have no registered ``repo_config``; their
        ledger is keyed on the ticket's own ``board_id`` (``"meta"``).
        Every other board uses ``ctx.repo_config.board_id``. This must
        match :class:`Settings.memory_file_for`'s non-empty requirement.
        """
        return ctx.repo_config.board_id if ctx.repo_config else ticket.board_id
