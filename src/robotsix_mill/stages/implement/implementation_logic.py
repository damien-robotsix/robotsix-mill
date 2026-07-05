"""Implementation-logic mixin: agent invocation, single pass, test/result evaluation."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from ...agents import coding
from ...agents.coding import AgentBudgetError, AgentRunError
from ...agents.coordinating import ValidationResult
from ...agents.testing import smoke_paths_match
from ...config import ConfigError, Settings, get_repo_config, target_branch_for
from ...core.models import Ticket
from ...core.states import State
from ...config.repo_settings import load_repo_smoke_command
from ...runners.pass_runner import persist_memory
from ...vcs import git_ops
from .. import short_circuit_verify
from ..base import Outcome, StageContext
from ..pause import (
    acknowledge_unanswered_threads,
)
from ._base import _ImplementStageBase
from ._shared import (
    _AgentRunOutcome,
    _ImplementContext,
    _SinglePassResult,
    _is_config_only_change,
    _is_rename_only_change,
    _is_spec_exact_edits,
    _parse_spec_code_blocks,
    _should_skip_test_gate,
    log,
)


class ImplementationLogicMixin(_ImplementStageBase):
    """Agent-driven coding passes for :class:`ImplementStage`."""

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
            cls._finalize(
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
        if passed:
            smoke_cmd = (
                load_repo_smoke_command(repo_dir) or settings.smoke_command
            ).strip()
            if smoke_cmd:
                changed = git_ops.introduced_files(repo_dir, target)
                smoke_paths = _facade.load_repo_smoke_paths(repo_dir)
                if smoke_paths_match(changed, smoke_paths):
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
                        passed = False
                        diag = smoke_diag
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
            if (
                not cls._any_repo_has_changes(
                    repo_dir, extra_roots, target, settings=settings
                )
                and no_change_needed
                and no_change_rationale.strip()
            ):
                # Edit-claim contradiction guard: the agent signalled
                # ``no_change_needed`` (with a rationale) yet the working
                # tree is empty. If the run actually INVOKED file-mutating
                # tools, the edits never persisted (reverted, workspace
                # reset mid-run, or written outside the clone) — closing as
                # DONE would silently lose real work and falsely complete the
                # ticket. This is exactly how ticket 904a (the ticket that
                # was meant to ADD this guard) was lost. BLOCK for inspection
                # instead of short-circuiting.
                edit_tools = short_circuit_verify.detect_edit_claim_contradiction(
                    has_changes=False, new_messages=new_msgs
                )
                # An empty diff after edit calls is usually lost work (BLOCK).
                # But it is legitimate when the edits were redundant or the
                # project formatter normalises them away — the canonical case
                # being a ticket that "fixes" valid PEP-758 ``except A, B:`` to
                # ``except (A, B):``, which ``ruff format`` reverts on a 3.14
                # target, so every edit nets to zero and the agent correctly
                # reports ``no_change_needed``. Replay the edits + format to
                # tell redundant-no-op from lost-work; only a confirmed no-op
                # (True) is allowed to close DONE. None/False → BLOCK as before.
                if edit_tools and (
                    cls._edits_formatter_reverted(repo_dir, new_msgs) is not True
                ):
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
            # Shared no-change summary — defined here so both the
            # empty-diff block below and the ceremonial-only guard
            # further down can reference it.
            no_change_summary = summary or (
                "Agent finished without producing any file edits and "
                "without explanation. Check artifacts/implement_messages.json "
                "for the full transcript."
            )
            if (
                not cls._any_repo_has_changes(
                    repo_dir, extra_roots, target, settings=settings
                )
                and not resuming
            ):
                # Empty diff on a fresh run: the working tree is clean AND
                # the branch has no commits beyond ``origin/<target>`` —
                # there is genuinely nothing to merge. This is the single
                # biggest source of BLOCKED-loop tickets: a spec already
                # satisfied on main loops implement → empty PR → close →
                # BLOCKED → resume forever. Resolve it deterministically
                # here — but ONLY when the empty diff is genuinely a
                # no-op, never when real work may have been lost.
                #
                # Two guards protect against silently closing real work:
                #
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
                #       no-op and is exempt (mirrors the ``no_change_needed``
                #       guard above).
                #
                # When NEITHER guard fires, the agent looked and made no
                # (surviving) edits: the spec is already satisfied. Close
                # DONE with a clear terminal note instead of looping.
                # Guard (a): gitignored-edit detector. Real writes into a
                # gitignored path (e.g. a manifest board whose ``.gitignore``
                # carries ``/src/*`` for vcs-imported sub-repos) are
                # invisible to ``git status`` and must NOT be closed as a
                # no-op.
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
                # Guard (b): edit-claim contradiction. The run invoked
                # edit tools but nothing survived → likely lost work.
                # Exempt only a confirmed formatter-reverted / redundant
                # no-op (True); None/False → BLOCK.
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
            # --- ceremonial-only change guard ---
            # We reach here when ``_any_repo_has_changes`` is True but the
            # agent may have only modified ceremonial files (e.g. called
            # ``insert_changelog_entry`` on a spec already satisfied on
            # main).  Check whether the working-tree diff contains ANY
            # non-ceremonial file; if not, the intent is already on main
            # and we should terminate DONE instead of opening an empty PR
            # that will be auto-closed and loop forever.
            #
            # Only fires when the branch is NOT ahead of main (no commits
            # beyond origin/target) — if there are real commits on the
            # branch, the net diff must be evaluated, not just the WT.
            if not resuming and not git_ops.branch_is_ahead_of_main(repo_dir, target):
                try:
                    diff_out = subprocess.run(
                        [
                            "git",
                            "-C",
                            str(repo_dir),
                            "diff",
                            "--name-only",
                            "HEAD",
                        ],
                        capture_output=True,
                        text=True,
                    ).stdout
                except Exception:
                    diff_out = ""
                wt_files = (
                    [f for f in diff_out.strip().split("\n") if f] if diff_out else []
                )
                substantive_wt = [
                    f for f in wt_files if f not in git_ops.CEREMONIAL_FILES
                ]
                if wt_files and not substantive_wt:
                    done_note = (
                        "already satisfied — ceremonial-only working-tree "
                        "changes (no substantive diff vs base)"
                    )
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
                        "%s: ceremonial-only WT changes — DONE (already satisfied)",
                        ticket.id,
                    )
                    return _SinglePassResult(
                        next_action="return",
                        outcome=Outcome(State.DONE, done_note),
                    )
            # --- per-claimed-file edit-claim verification ---
            # We reach here only on a non-empty-diff proceed (the two
            # no-change branches above returned when
            # ``_any_repo_has_changes`` was False). The sibling
            # ``detect_edit_claim_contradiction`` guard only fires on a
            # WHOLLY empty diff; it does NOT catch the case where the bulk
            # of the work is real but a few specifically-named sub-fixes
            # lag the summary/thread-reply (edits reverted, written outside
            # the clone, or simply never made). When that slips through,
            # the agent posts a comment asserting edits the diff lacks and
            # review re-flags the persisting issue, burning extra
            # review→implement rounds. Catch it HERE — before the comment
            # is posted (acknowledge_unanswered_threads) and before the
            # handoff to review — anchored deterministically on the
            # edit-tool-call path args cross-referenced against the net
            # diff (no NL/symbol parsing).
            changed = git_ops.introduced_files(repo_dir, target)
            if extra_roots:
                for repo_path in extra_roots:
                    # Mirror _any_repo_has_changes: the primary repo is
                    # already covered above; skip the duplicate entry.
                    if repo_path == repo_dir:
                        continue
                    try:
                        rc = get_repo_config(repo_path.name)
                    except ConfigError:
                        rc = None
                    repo_target = target_branch_for(settings, rc)
                    changed = list(
                        set(changed)
                        | set(git_ops.introduced_files(repo_path, repo_target))
                    )
            missing = short_circuit_verify.detect_missing_claimed_files(
                changed_files=changed,
                new_messages=new_msgs,
                summary=summary,
            )
            if missing:
                file_list = ", ".join(missing)
                diag = (
                    "[Diagnostic] Your summary / thread-reply claims edits to "
                    f"the following file(s) — {file_list} — but they are ABSENT "
                    "from the net diff vs "
                    f"origin/{target}. An edit-tool-call "
                    "targeted each of them and your summary names them as fixed, "
                    "yet the working tree does not contain those changes (edits "
                    "reverted, written outside the clone, or never applied). "
                    "Before completing, actually apply those edits so they land "
                    "in the diff — OR correct your summary so it does not claim "
                    "edits you did not make. Do not hand un-landed claims to "
                    "review."
                )
                if attempt < max_iters:
                    # Iterations remain → re-prompt via the established retry
                    # path; it loops back into _run_single_implement_pass.
                    new_ic.feedback = diag
                    return _SinglePassResult(
                        next_action="retry",
                        feedback=diag,
                        ic=new_ic,
                        new_msgs=new_msgs,
                    )
                # Iterations exhausted → do NOT hand un-landed claims to
                # review. BLOCK for inspection, mirroring the empty-diff
                # contradiction guard's shape.
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
                        "edit-claim contradiction (claimed files absent from diff)",
                    ),
                )

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
    def _handle_rename_only_change(
        cls,
        ctx: StageContext,
        ticket: Ticket,
        repo_dir: Path,
        branch: str,
        settings: Settings,
        ic: _ImplementContext,
        target: str,
        extra_roots: list[Path] | None,
    ) -> _SinglePassResult:
        """Handle a rename-only change deterministically — no LLM invocation.

        Collects the rename list for the summary, persists artifacts,
        runs the scope guardrail, and routes to test evaluation (which
        will skip via :func:`_should_skip_test_gate`).
        """
        import subprocess as sp

        # Collect renamed files for the summary.
        rename_out = sp.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "diff",
                "--diff-filter=R",
                "--name-only",
                f"origin/{target}",
            ],
            capture_output=True,
            text=True,
        )
        renamed: list[str] = (
            rename_out.stdout.strip().splitlines() if rename_out.returncode == 0 else []
        )

        # Collect all changed files for reference_files.
        all_out = sp.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "diff",
                "--name-only",
                f"origin/{target}",
            ],
            capture_output=True,
            text=True,
        )
        all_changed: list[str] = (
            all_out.stdout.strip().splitlines() if all_out.returncode == 0 else []
        )

        # Build a deterministic summary.
        renamed_preview = ", ".join(renamed[:5])
        if len(renamed) > 5:
            renamed_preview += f" (+{len(renamed) - 5} more)"
        summary = f"rename-only change: {len(renamed)} file(s) renamed" + (
            f" — {renamed_preview}" if renamed_preview else ""
        )

        ws = ctx.service.workspace(ticket)
        memory_board_id = cls._memory_board_id(ctx, ticket)

        # Persist artifacts (no memory update — no agent ran).
        ref_files = all_changed
        cls._persist_pass_artifacts(
            ws,
            ticket,
            ic,
            summary,
            ref_files,
            "",
            settings,
            memory_board_id,
        )

        # Run scope guardrail.
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
            return _SinglePassResult(next_action="return", outcome=guardrail.outcome)

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
            reference_files=[{"path": p} for p in ref_files],
            file_map=new_file_map,
            feedback=new_feedback,
            previous_attempt_summary=summary,
            open_thread_ids=ic.open_thread_ids,
        )
        if guardrail.action == "continue":
            return _SinglePassResult(next_action="retry", feedback=None, ic=new_ic)

        # Route to test evaluation (which will skip via _should_skip_test_gate).
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
            None,  # new_msgs
            False,  # no_change_needed
            "",  # no_change_rationale
            False,  # resuming
            1,  # attempt
            max(1, settings.max_fix_iterations),  # max_iters
            extra_roots,
        )

    @classmethod
    def _handle_spec_exact_edits(
        cls,
        ctx: StageContext,
        ticket: Ticket,
        repo_dir: Path,
        branch: str,
        settings: Settings,
        ic: _ImplementContext,
        target: str,
        extra_roots: list[Path] | None,
    ) -> _SinglePassResult:
        """Handle a spec-exact-code ticket deterministically — no LLM invocation.

        Parses fenced code blocks annotated with file paths from the
        ticket description, applies them as edits (unified-diff or
        context-aware replacement), then runs the scope guardrail and
        routes to test evaluation.
        """
        import difflib
        import subprocess as sp

        blocks = _parse_spec_code_blocks(ic.spec)

        applied: list[str] = []
        failed: list[str] = []
        changed_files: set[str] = set()

        for file_path, _info, code in blocks:
            target_file = repo_dir / file_path
            if not target_file.is_file():
                failed.append(f"{file_path}: file not found")
                continue

            original = target_file.read_text()

            # --- Strategy 1: unified diff ---------------------------------
            if (
                code.startswith("--- ")
                or code.startswith("+++ ")
                or code.startswith("@@")
            ):
                try:
                    result = sp.run(
                        ["patch", "--batch", "-p0", "-o", "-", str(target_file)],
                        input=code,
                        capture_output=True,
                        text=True,
                        cwd=str(repo_dir),
                        timeout=10,
                    )
                    if result.returncode == 0 and result.stdout:
                        target_file.write_text(result.stdout)
                        applied.append(file_path)
                        changed_files.add(file_path)
                        continue
                except Exception as exc:
                    log.debug(
                        "Spec-exact: unified diff failed for %s: %s", file_path, exc
                    )

            # --- Strategy 2: context-aware replacement --------------------
            # Split both into lines and find the longest matching region.
            file_lines = original.splitlines(keepends=True)
            code_lines = code.splitlines(keepends=True)
            code_stripped = [line.rstrip("\n\r") for line in code_lines]

            sm = difflib.SequenceMatcher(
                None,
                [line.rstrip("\n\r") for line in file_lines],
                code_stripped,
            )
            match = sm.find_longest_match(0, len(file_lines), 0, len(code_stripped))

            # Require at least 2 matching lines (or 1 if the code block
            # is only 1 line) to consider it a context match.
            min_match = min(2, len(code_stripped))
            if match.size >= min_match:
                # Replace the matched region with the full code block.
                new_lines = (
                    file_lines[: match.a]
                    + code_lines
                    + file_lines[match.a + match.size :]
                )
                new_content = "".join(new_lines)
                if new_content != original:
                    target_file.write_text(new_content)
                    applied.append(file_path)
                    changed_files.add(file_path)
                    continue

            # --- Strategy 3: insertion via context hints ------------------
            # Look for insertion-point hints in the lines preceding the
            # code block in the spec.
            insertion_point = cls._find_insertion_point(ic.spec, code, file_lines)
            if insertion_point is not None:
                new_lines = (
                    file_lines[:insertion_point]
                    + code_lines
                    + file_lines[insertion_point:]
                )
                new_content = "".join(new_lines)
                target_file.write_text(new_content)
                applied.append(file_path)
                changed_files.add(file_path)
                continue

            failed.append(f"{file_path}: could not determine edit location")

        if not applied:
            # Nothing was applied — fall through to LLM path via retry.
            # Include a sentinel ``_ImplementContext`` so
            # ``_select_agent_level`` detects the prior failed attempt
            # and returns ``None`` (→ LLM path) instead of ``-1``,
            # breaking what would otherwise be an infinite retry loop
            # (spec is static, so ``_is_spec_exact_edits`` keeps
            # returning ``True`` across retries).
            log.warning(
                "Spec-exact bypass: no edits applied (%d block(s) failed: %s)",
                len(failed),
                ", ".join(failed[:5]),
            )
            fail_summary = (
                f"spec-exact bypass: failed — {len(failed)} block(s) unapplied"
            )
            return _SinglePassResult(
                next_action="retry",
                feedback=(
                    "Spec-exact bypass: could not apply any edits. "
                    + f"Failed: {', '.join(failed[:3])}"
                ),
                ic=_ImplementContext(
                    spec=ic.spec,
                    memory_text=ic.memory_text,
                    reference_files=ic.reference_files,
                    file_map=ic.file_map,
                    feedback=ic.feedback,
                    previous_attempt_summary=fail_summary,
                    open_thread_ids=ic.open_thread_ids,
                ),
            )

        # Build a deterministic summary.
        applied_preview = ", ".join(applied[:5])
        if len(applied) > 5:
            applied_preview += f" (+{len(applied) - 5} more)"
        summary = f"spec-exact edit: {len(applied)} file(s) changed — {applied_preview}"

        if failed:
            summary += f" ({len(failed)} block(s) skipped)"

        ws = ctx.service.workspace(ticket)
        memory_board_id = cls._memory_board_id(ctx, ticket)

        ref_files = sorted(changed_files)

        # Persist artifacts (no memory update — no agent ran).
        cls._persist_pass_artifacts(
            ws,
            ticket,
            ic,
            summary,
            ref_files,
            "",
            settings,
            memory_board_id,
        )

        # Run scope guardrail.
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
            return _SinglePassResult(next_action="return", outcome=guardrail.outcome)

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
            reference_files=[{"path": p} for p in ref_files],
            file_map=new_file_map,
            feedback=new_feedback,
            previous_attempt_summary=summary,
            open_thread_ids=ic.open_thread_ids,
        )
        if guardrail.action == "continue":
            return _SinglePassResult(next_action="retry", feedback=None, ic=new_ic)

        # Route to test evaluation.
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
            None,  # new_msgs
            False,  # no_change_needed
            "",  # no_change_rationale
            False,  # resuming
            1,  # attempt
            max(1, settings.max_fix_iterations),  # max_iters
            extra_roots,
        )

    @staticmethod
    def _find_insertion_point(
        spec: str,
        code: str,
        file_lines: list[str],
    ) -> int | None:
        """Try to determine where in *file_lines* to insert *code* from *spec* context.

        Looks at the text preceding the code block in *spec* for hints:
        - "after the imports" / "after imports" → after last import line
        - "after line N" → after line N
        - "before line N" → before line N
        - "at the end" / "end of file" → at end
        - "before class"/"before def" → before first class/def

        Returns a 0-based line index or ``None`` if no hint was found.
        """
        import re as _re

        # Find the code block in the spec to get its preceding context.
        escaped = _re.escape(code[:80])
        pattern = _re.compile(r"(.*?)```\w*\n" + escaped, _re.DOTALL)
        m = pattern.search(spec)
        if not m:
            return None

        before = m.group(1)
        # Take the last 10 lines of preceding context.
        context_lines = before.split("\n")[-10:]
        context = "\n".join(context_lines)

        # "after the imports" / "after imports"
        if _re.search(r"after\s+(the\s+)?imports?", context, _re.IGNORECASE):
            for i in range(len(file_lines) - 1, -1, -1):
                stripped = file_lines[i].lstrip()
                if stripped.startswith(("import ", "from ")):
                    return i + 1
            # No imports found — insert at top.
            return 0

        # "after line N"
        lm = _re.search(r"after\s+line\s+(\d+)", context, _re.IGNORECASE)
        if lm:
            n = int(lm.group(1))
            return min(n, len(file_lines))

        # "before line N"
        lm = _re.search(r"before\s+line\s+(\d+)", context, _re.IGNORECASE)
        if lm:
            n = int(lm.group(1))
            return max(0, n - 1)

        # "at the end" / "end of file" / "append"
        if _re.search(
            r"(at\s+the\s+end|end\s+of\s+file|append|bottom)",
            context,
            _re.IGNORECASE,
        ):
            return len(file_lines)

        # "before class X" / "before the class"
        if _re.search(r"before\s+(the\s+)?class\b", context, _re.IGNORECASE):
            for i, line in enumerate(file_lines):
                if line.lstrip().startswith("class "):
                    return i
            return None

        # "before def X" / "before the function"
        if _re.search(r"before\s+(the\s+)?(def|function)\b", context, _re.IGNORECASE):
            for i, line in enumerate(file_lines):
                if line.lstrip().startswith("def "):
                    return i
            return None

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
