"""Implementation-logic mixin: agent invocation, single pass, test/result evaluation."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from ...agents import coding
from ...agents.coding import AgentBudgetError, AgentRunError
from ...agents.coordinating import ValidationResult
from ...agents.testing import smoke_paths_match
from ...config import Settings, target_branch_for
from ...core.models import Ticket
from ...core.states import State
from ...config.repo_settings import load_repo_smoke_command
from ...runners.pass_runner import persist_memory
from ...vcs import git_ops
from .. import short_circuit_verify
from ..base import Outcome, StageContext
from ..pause import (
    acknowledge_unanswered_threads,
    save_conversation_state,
)
from ._base import _ImplementStageBase
from ._shared import (
    _AgentRunOutcome,
    _ImplementContext,
    _SinglePassResult,
    _is_config_only_change,
    _is_rename_only_change,
    _is_spec_exact_edits,
    _should_skip_test_gate,
    log,
)
from .implementation_editing import _ImplementationEditingMixin


class ImplementationLogicMixin(_ImplementationEditingMixin, _ImplementStageBase):
    """Agent-driven coding passes for :class:`ImplementStage`.

    Special-case edit handlers (:class:`_verify_repo_changes`,
    :class:`_handle_rename_only_change`, :class:`_handle_spec_exact_edits`,
    :class:`_find_insertion_point`) are provided by
    :class:`._implementation_editing._ImplementationEditingMixin`.
    """

    @classmethod
    def _select_agent_level(
        cls,
        ic: _ImplementContext,
        settings,
        repo_dir: Path,
        target_branch: str,
    ) -> int | None:
        """Pick the cheaper level-1 model for simple tickets, or bypass LLM
        entirely for rename-only and spec-exact-code tickets.

        Returns ``0`` for:
        * a rename-only change (every non-rename change is a config/doc
          stub or zero-delta file) — bypass the LLM coordinator entirely.

        Returns ``-1`` for:
        * a spec-exact-code ticket — the description contains fenced code
          blocks with file paths referencing existing files, so edits can
          be applied deterministically without an LLM.

        Returns ``1`` for:
        * a no-change-needed re-check (the previous attempt already
          concluded ``no_change_needed`` — pure re-check with the flash
          model); or
        * a config/docs-only ticket (every changed file is ``.md``,
          ``.yaml``, ``.toml``, etc. — no code to test).

        Returns ``None`` otherwise (keep the default level-2 model).
        """
        prev = (ic.previous_attempt_summary or "") + (ic.feedback or "")
        if "no change needed" in prev.lower():
            return 1
        if _is_config_only_change(repo_dir, target_branch):
            return 1
        if _is_rename_only_change(repo_dir, target_branch):
            return 0
        if _is_spec_exact_edits(ic.spec, repo_dir):
            # Sentinel check: if a prior spec-exact attempt already
            # failed (no edits applied), fall through to the LLM path
            # instead of re-entering the same doomed deterministic path.
            if ic.previous_attempt_summary and ic.previous_attempt_summary.startswith(
                "spec-exact bypass: failed"
            ):
                return None
            return -1
        return None

    @classmethod
    def _invoke_implement_agent(
        cls,
        ctx: StageContext,
        ticket: Ticket,
        repo_dir: Path,
        branch: str,
        settings,
        ic: _ImplementContext,
        language_instructions: str,
        agent_level: int | None,
        resume_history: list | None,
        extra_roots: list[Path] | None,
        memory_board_id: str,
        ws=None,  # Workspace — needed for save_conversation_state on budget error
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
                board_id=memory_board_id,
                current_ticket_id=ticket.id,
                message_history=resume_history,
                language_instructions=language_instructions,
                extra_roots=extra_roots,
                level=agent_level,
                sandbox_image=ctx.repo_config.sandbox_image
                if ctx.repo_config
                else None,
            )
        except AgentBudgetError as e:
            if e.conversation_state is not None and ws is not None:
                save_conversation_state(ws, e.conversation_state, "implement")
            cls._finalize(
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
            # If the original cause is a transient infra failure
            # (OpenRouter timeout, 5xx, 429, disk-full, …), re-raise
            # the typed cause so the worker's classify_stage_error
            # picks it up and schedules a retry-with-backoff via
            # set_retry_state.  Do NOT call _finalize first — that
            # would persist a spec fingerprint and poison the next
            # pass with a false "spec unchanged" block.
            if e.cause is not None:
                from ...runtime.transient_errors import (
                    classify_stage_error,
                    is_insufficient_credit,
                    parse_credit_shortfall,
                )

                if is_insufficient_credit(e.cause):
                    from ...runtime.credit_status import record_low_credit

                    detail = parse_credit_shortfall(e.cause)
                    record_low_credit(detail=detail)

                if classify_stage_error(e.cause) == "transient":
                    raise e.cause from e
            # Non-transient agent error — record the outcome (spec-
            # determined dead-end) so the fingerprint guard can block
            # a re-spawn with an unchanged spec.
            cls._finalize(
                ctx,
                ticket,
                repo_dir,
                branch,
                f"agent error: {e}",
                ok=False,
                extra_roots=extra_roots,
            )
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

    @classmethod
    def _persist_pass_artifacts(
        cls,
        ws,
        ticket: Ticket,
        ic: _ImplementContext,
        summary: str,
        ref_files: list[str] | None,
        updated_memory: str,
        settings,
        memory_board_id: str,
    ) -> tuple[list | None, str | None]:
        """Persist memory, ``reference_files.json`` and ``implement_summary.md``."""
        if updated_memory:
            persist_memory(
                settings.memory_file_for("implement", memory_board_id),
                updated_memory,
            )

        # Build updated reference_files for the context.
        updated_ref_files = ic.reference_files
        if ref_files:
            updated_ref_files = [{"path": p} for p in ref_files]
            try:
                ref_path = ws.artifacts_dir / "reference_files.json"
                ref_path.write_text(
                    json.dumps(updated_ref_files, indent=2),
                    encoding="utf-8",
                )
            except OSError:
                log.warning(
                    "%s: failed to write reference_files.json",
                    ticket.id,
                    exc_info=True,
                )

        # Persist summary for <previous_attempt> injection on retry.
        updated_prev_summary = ic.previous_attempt_summary
        try:
            (ws.artifacts_dir / "implement_summary.md").write_text(
                summary,
                encoding="utf-8",
            )
            updated_prev_summary = summary
        except OSError:
            log.warning(
                "%s: failed to write implement_summary.md",
                ticket.id,
                exc_info=True,
            )

        return updated_ref_files, updated_prev_summary

    @classmethod
    def _evaluate_test_results(
        cls,
        ctx: StageContext,
        ticket: Ticket,
        repo_dir: Path,
        branch: str,
        settings,
        ic: _ImplementContext,
        new_ic: _ImplementContext,
        summary: str,
        ref_files: list[str] | None,
        new_msgs,
        no_change_needed: bool,
        no_change_rationale: str,
        resuming: bool,
        attempt: int,
        max_iters: int,
        extra_roots: list[Path] | None,
    ) -> _SinglePassResult:
        """Run the test gate, apply ``ValidationResult.decide``, route the pass."""
        target = target_branch_for(settings, ctx.repo_config)
        from robotsix_mill.stages import implement as _facade

        ticket_summary = (ic.spec or ticket.title or "")[:200]
        skip, skip_diag = _should_skip_test_gate(
            repo_dir, target, settings, ticket_summary
        )
        if skip:
            passed, diag = True, skip_diag
        else:
            passed, diag = _facade.run_test_agent(
                settings=settings,
                repo_dir=repo_dir,
                repo_config=ctx.repo_config,
            )
        # --- path-scoped smoke gate (runs ONLY after unit tests pass) ---
        # No point smoking a red build; a smoke failure folds into the
        # SAME passed/diag → ValidationResult.decide machinery as a test
        # failure (retry while iterations remain, escalate on the last,
        # BLOCKED on sandbox-unavailable). Strictly opt-in: skipped
        # entirely unless a smoke command is set (repo file wins over the
        # global fallback), and skipped when the ticket's introduced
        # files don't match the repo's smoke_paths globs.
        passed, diag = cls._run_smoke_gate(
            ctx, ticket, repo_dir, target, settings, passed, diag
        )
        if not passed and diag.startswith("sandbox unavailable"):
            cls._finalize(
                ctx,
                ticket,
                repo_dir,
                branch,
                summary,
                ok=False,
                reference_files=ref_files,
                extra_roots=extra_roots,
            )
            return _SinglePassResult(
                next_action="return",
                outcome=Outcome(State.BLOCKED, diag),
            )

        decision = ValidationResult.decide(
            passed=passed,
            iterations=attempt,
            max_iters=max_iters,
            feedback=diag,
        )

        if decision.next_action == "proceed":
            # ``no_change_needed`` → DONE works on both fresh runs and
            # resumes. The agent's signal that the spec is already
            # satisfied is meaningful regardless of how we got here; in
            # fact the resume case is exactly the bc-check
            # "remove-dead-X" flavour where a human unblocked the
            # ticket precisely because they suspect the work was
            # already landed by a sibling.
            #
            # Guard against a resume-case false positive: when the
            # branch carries commits ahead of ``origin/main`` (the
            # agent's previous iterations already produced the diff),
            # routing to DONE silently strands that work in the
            # workspace — it never reaches deliver, no PR is opened.
            # Treat that as a normal proceed instead of a no-change
            # bypass; deliver will pick it up.
            # --- no-change contradiction detection ---
            # Two branches: (a) agent explicitly signalled no_change_needed,
            # (b) empty diff on a fresh run.  Both route through the same
            # edit-claim / gitignored-edit / formatter-reverted guards.
            no_change_result = cls._detect_no_change_contradiction(
                ctx,
                ticket,
                repo_dir,
                branch,
                settings,
                summary,
                ref_files,
                new_msgs,
                no_change_needed,
                no_change_rationale,
                resuming,
                target,
                extra_roots,
                attempt=attempt,
                max_iters=max_iters,
                new_ic=new_ic,
            )
            if no_change_result is not None:
                return no_change_result
            # --- per-claimed-file & zero-tool-call guards ---
            verify_result = cls._verify_repo_changes(
                ctx,
                ticket,
                repo_dir,
                branch,
                settings,
                summary,
                ref_files,
                new_msgs,
                new_ic,
                ic,
                target,
                extra_roots,
                resuming,
                attempt,
                max_iters,
            )
            if verify_result is not None:
                return verify_result

            # --- post-agent thread acknowledgment ---
            if ic.open_thread_ids and ic.feedback:
                acknowledge_unanswered_threads(ctx, ticket, ic.open_thread_ids)
            cls._finalize(
                ctx,
                ticket,
                repo_dir,
                branch,
                summary,
                ok=True,
                reference_files=ref_files,
                extra_roots=extra_roots,
            )
            next_state = (
                State.CODE_REVIEW if settings.review_enabled else State.DOCUMENTING
            )
            # Same-state step event so implement gets its own visible
            # row in history. Without this, the ticket's history shows
            # `ready -> code_review` (or `ready -> documenting`) and
            # the implement summary lives on the code_review/documenting
            # row — fine on inspection, but the row reads as the
            # downstream stage rather than what implement just did.
            # The downstream Outcome's note is a short stage-name
            # marker; the full summary lives on the step event (and
            # in artifacts/implement.md).
            ctx.service.add_step_event(
                ticket.id,
                f"implement: {summary[:400]}",
            )
            next_note = (
                "code review starting"
                if next_state is State.CODE_REVIEW
                else "documenting starting"
            )
            # Increment the ticket-lifetime implement-cycle counter
            # so the convergence backstop in phase_coordinator can
            # catch a runaway implement↔review loop.
            if next_state is State.CODE_REVIEW:
                ctx.service.set_implement_cycles(ticket.id, ticket.implement_cycles + 1)
            return _SinglePassResult(
                next_action="proceed",
                outcome=Outcome(next_state, next_note),
            )

        if decision.next_action == "escalate":
            cls._finalize(
                ctx,
                ticket,
                repo_dir,
                branch,
                summary,
                ok=False,
                reference_files=ref_files,
                extra_roots=extra_roots,
            )
            return _SinglePassResult(
                next_action="escalate",
                outcome=Outcome(
                    State.BLOCKED,
                    f"tests still failing after {max_iters} fix "
                    "attempt(s) — resumable (move to READY)",
                ),
            )

        # retry → feed the diagnosis into the next edit pass.
        new_ic.feedback = diag
        return _SinglePassResult(
            next_action="retry",
            feedback=diag,
            ic=new_ic,
            new_msgs=new_msgs,
        )

    @classmethod
    def _run_smoke_gate(
        cls,
        ctx: StageContext,
        ticket: Ticket,
        repo_dir: Path,
        target: str,
        settings: Settings,
        passed: bool,
        diag: str,
    ) -> tuple[bool, str]:
        """Run the smoke-test gate when unit tests pass.

        Opt-in per-repo via ``smoke_command`` and ``smoke_paths``;
        skipped entirely when the ticket's introduced files don't
        match the repo's ``smoke_paths`` globs.

        Returns ``(passed, diag)`` unchanged when the smoke gate is
        skipped or passes; folds a smoke failure into
        ``(False, smoke_diag)``.
        """
        if not passed:
            return passed, diag
        smoke_cmd = (
            load_repo_smoke_command(repo_dir) or settings.smoke_command
        ).strip()
        if not smoke_cmd:
            return passed, diag
        from robotsix_mill.stages import implement as _facade

        changed = git_ops.introduced_files(repo_dir, target)
        smoke_paths = _facade.load_repo_smoke_paths(repo_dir)
        if not smoke_paths_match(changed, smoke_paths):
            return passed, diag
        smoke_passed, smoke_diag = _facade.run_smoke_agent(
            settings=settings,
            repo_dir=repo_dir,
            repo_config=ctx.repo_config,
        )
        # The board browser smoke writes its screenshot to
        # ``<clone>/artifacts/board.png`` (BOARD_SMOKE_SCREENSHOT,
        # relative to the sandbox cwd = the repo clone, the only
        # writable mount). The review stage reads it from the
        # workspace artifacts dir — a sibling of the clone, outside
        # the sandbox mount — so lift it out here. Absent for
        # non-board smokes / a failed render → review stays
        # text-only, unchanged.
        # MOVE (not copy) so the screenshot never lingers in
        # the clone's working tree — otherwise ``_finalize``'s
        # ``git add -A`` would stage and commit it into the
        # feature branch (``.png`` is not a binary artifact and
        # the smoke runs past the scope guardrail).
        ws = ctx.service.workspace(ticket)
        src_png = repo_dir / "artifacts" / "board.png"
        if src_png.exists():
            shutil.move(str(src_png), str(ws.artifacts_dir / "board.png"))
        if not smoke_passed:
            return False, smoke_diag
        return passed, diag

    @classmethod
    def _detect_no_change_contradiction(
        cls,
        ctx: StageContext,
        ticket: Ticket,
        repo_dir: Path,
        branch: str,
        settings: Settings,
        summary: str,
        ref_files: list[str] | None,
        new_msgs: bytes | None,
        no_change_needed: bool,
        no_change_rationale: str,
        resuming: bool,
        target: str,
        extra_roots: list[Path] | None,
        attempt: int = 1,
        max_iters: int = 1,
        new_ic: _ImplementContext | None = None,
    ) -> _SinglePassResult | None:
        """Check for edit-claim contradictions when the diff is empty.

        An agent that invokes file-mutating tools yet claims
        ``no_change_needed`` (or produces an empty diff on a fresh
        run) is signalling lost work — BLOCK for inspection instead
        of closing DONE.  A confirmed formatter-reverted / redundant
        edit is exempt (a true no-op).

        Returns a ``_SinglePassResult`` when a contradiction is found
        (caller must return it immediately); ``None`` when the empty
        diff is legitimate and the caller should close DONE.
        """
        _ = settings  # unused in this helper
        if not cls._any_repo_has_changes(
            repo_dir, extra_roots, target, settings=settings
        ):
            if no_change_needed and no_change_rationale.strip():
                # Agent explicitly signalled no_change_needed.
                edit_tools = short_circuit_verify.detect_edit_claim_contradiction(
                    has_changes=False, new_messages=new_msgs
                )
                if edit_tools:
                    fmt_result = cls._edits_formatter_reverted(repo_dir, new_msgs)
                    if fmt_result is not True:
                        tool_list = ", ".join(edit_tools)
                        diag = (
                            f"{no_change_rationale.strip() or summary}\n\n"
                            "[Diagnostic] implement was about to close this ticket "
                            "as ``no_change_needed`` because ``git diff`` is empty "
                            f"— but the agent invoked file-mutating tools "
                            f"({tool_list}) during the run, and replaying those "
                            "edits + formatting still produced a real change (or "
                            "could not be verified). An empty diff after real edit "
                            "calls means the work did NOT persist (edits reverted, "
                            "workspace reset mid-run, or written outside the clone). "
                            "Closing as no-change would silently lose that work, so "
                            "the ticket is BLOCKED for inspection. Re-run implement; "
                            "if the spec genuinely needs no change, the agent must "
                            "reach that conclusion WITHOUT calling "
                            "write_file/edit_file/Write/Edit."
                        )
                        cls._finalize(
                            ctx,
                            ticket,
                            repo_dir,
                            branch,
                            diag,
                            ok=False,
                            reference_files=ref_files,
                            extra_roots=extra_roots,
                        )
                        return _SinglePassResult(
                            next_action="return",
                            outcome=Outcome(
                                State.BLOCKED,
                                "edit-claim contradiction (empty diff after edit calls)",
                            ),
                        )
                # No contradiction — close DONE.
                rationale = no_change_rationale.strip()
                short = rationale[:400] + ("…" if len(rationale) > 400 else "")
                cls._finalize(
                    ctx,
                    ticket,
                    repo_dir,
                    branch,
                    f"no change needed — {rationale}",
                    ok=True,
                    reference_files=ref_files,
                    extra_roots=extra_roots,
                )
                return _SinglePassResult(
                    next_action="return",
                    outcome=Outcome(State.DONE, f"no change needed — {short}"),
                )
            if not resuming:
                # Empty diff on a fresh run: the working tree is clean AND
                # the branch has no commits beyond ``origin/<target>`` —
                # there is genuinely nothing to merge.
                #
                # Two guards protect against silently closing real work:
                #   (a) gitignored edits — real writes into a gitignored
                #       path are invisible to ``git status`` and surface
                #       as an opaque empty diff. Closing DONE would lose
                #       deliverable work → BLOCK.
                #   (b) edit-claim contradiction — the run invoked
                #       file-mutating tools yet nothing landed (edits
                #       reverted, workspace reset mid-run, or written off
                #       clone). Closing DONE would lose that work → BLOCK.
                #       A confirmed formatter-reverted / redundant edit
                #       (``_edits_formatter_reverted`` is True) is a true
                #       no-op and is exempt.
                no_change_summary = summary or (
                    "Agent finished without producing any file edits and "
                    "without explanation. Check artifacts/implement_messages.json "
                    "for the full transcript."
                )
                # Guard (a): gitignored-edit detector.
                ignored_hits = cls._claimed_gitignored_edits(repo_dir, new_msgs)
                if ignored_hits:
                    hit_list = ", ".join(f"`{p}`" for p in ignored_hits)
                    no_change_summary = (
                        f"edits landed in gitignored path(s): {hit_list} — the "
                        "files exist on disk but git cannot see them, so this "
                        "board cannot deliver them (vcs-imported / vendored "
                        "sub-tree). The spec must target git-tracked files, or "
                        "the board needs manifest-aware delivery for that "
                        f"sub-tree.\n\n{no_change_summary}"
                    )
                    cls._finalize(
                        ctx,
                        ticket,
                        repo_dir,
                        branch,
                        no_change_summary,
                        ok=False,
                        reference_files=ref_files,
                        extra_roots=extra_roots,
                    )
                    reason = " ".join(no_change_summary.split())
                    return _SinglePassResult(
                        next_action="return",
                        outcome=Outcome(
                            State.BLOCKED,
                            f"no changes produced — {reason[:300]}"
                            + ("… (see implement.md)" if len(reason) > 300 else ""),
                        ),
                    )
                # Guard (b): edit-claim contradiction.
                edit_tools = short_circuit_verify.detect_edit_claim_contradiction(
                    has_changes=False, new_messages=new_msgs
                )
                if edit_tools and (
                    cls._edits_formatter_reverted(repo_dir, new_msgs) is not True
                ):
                    tool_list = ", ".join(edit_tools)
                    diag = (
                        f"{no_change_summary}\n\n"
                        "[Diagnostic] implement produced an empty diff, but the "
                        f"agent invoked file-mutating tools ({tool_list}) during "
                        "the run and replaying those edits + formatting still "
                        "produced a real change (or could not be verified). An "
                        "empty diff after real edit calls means the work did NOT "
                        "persist (edits reverted, workspace reset mid-run, or "
                        "written outside the clone). Closing as no-change would "
                        "silently lose that work, so the ticket is BLOCKED for "
                        "inspection."
                    )
                    cls._finalize(
                        ctx,
                        ticket,
                        repo_dir,
                        branch,
                        diag,
                        ok=False,
                        reference_files=ref_files,
                        extra_roots=extra_roots,
                    )
                    return _SinglePassResult(
                        next_action="return",
                        outcome=Outcome(
                            State.BLOCKED,
                            "edit-claim contradiction (empty diff after edit calls)",
                        ),
                    )
                # Genuine no-op: clean working tree, no commits beyond the
                # base, no gitignored writes, no lost edits. The spec is
                # already satisfied — terminate DONE instead of looping.
                done_note = "already satisfied — no changes needed (empty diff vs base)"
                cls._finalize(
                    ctx,
                    ticket,
                    repo_dir,
                    branch,
                    f"{done_note}\n\n{no_change_summary}",
                    ok=True,
                    reference_files=ref_files,
                    extra_roots=extra_roots,
                )
                log.info(
                    "%s: empty diff on fresh run with no lost work — DONE "
                    "(already satisfied)",
                    ticket.id,
                )
                return _SinglePassResult(
                    next_action="return",
                    outcome=Outcome(State.DONE, done_note),
                )
        return None

    @classmethod
    def _run_single_implement_pass(
        cls,
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
        memory_board_id = cls._memory_board_id(ctx, ticket)

        language_instructions = cls._resolve_language_instructions(
            ctx,
            ticket,
            settings,
        )
        target = target_branch_for(settings, ctx.repo_config)
        agent_level = cls._select_agent_level(ic, settings, repo_dir, target)

        # Rename-only changes bypass the LLM coordinator entirely.
        if agent_level == 0:
            return cls._handle_rename_only_change(
                ctx,
                ticket,
                repo_dir,
                branch,
                settings,
                ic,
                target,
                extra_roots,
            )

        # Spec-exact-code tickets bypass the LLM coordinator entirely.
        if agent_level == -1:
            return cls._handle_spec_exact_edits(
                ctx,
                ticket,
                repo_dir,
                branch,
                settings,
                ic,
                target,
                extra_roots,
            )

        agent_result = cls._invoke_implement_agent(
            ctx,
            ticket,
            repo_dir,
            branch,
            settings,
            ic,
            language_instructions,
            agent_level,
            resume_history,
            extra_roots,
            memory_board_id,
            ws,
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

        pause = cls._maybe_handle_pause(
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

        updated_ref_files, updated_prev_summary = cls._persist_pass_artifacts(
            ws,
            ticket,
            ic,
            summary,
            ref_files,
            updated_memory,
            settings,
            memory_board_id,
        )

        guardrail = cls._run_scope_guardrail(
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
                new_msgs=new_msgs,
            )

        # guardrail.action == "skip_iteration" — fall through to test gate.
        return cls._evaluate_test_results(
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

    # ------------------------------------------------------------------
    # prerequisite gate
    # ------------------------------------------------------------------
