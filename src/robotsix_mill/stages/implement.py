"""Implement stage: READY -> DELIVERABLE (or BLOCKED, resumable).

First run: clone the target repo into the ticket workspace, branch,
then run a deterministic, stage-owned fix loop: invoke the implement
agent for one edit pass, run the test gate, and — on failure — re-invoke
the agent with a distilled diagnosis. The routing (proceed / retry /
escalate) is decided in Python (see
:class:`~..agents.coordinating.ValidationResult`), bounded by
``settings.max_fix_iterations``. Pass -> DELIVERABLE.

Resume: if the ticket workspace already has the clone + its branch (a
prior BLOCKED run), do NOT re-clone — check the branch out and continue
from the committed WIP.

Everything that isn't success is BLOCKED-resumable with WIP committed:
no remote, clone failure, no changes, sandbox down, agent error/budget
cap, or tests still failing after ``max_fix_iterations``. Pushing the
branch + opening the MR happens later, in the deliver stage.
"""

from __future__ import annotations

import json
import logging
import re as _re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..agents import coding
from ..agents.coding import AgentBudgetError, AgentRunError
from ..agents.coordinating import ValidationResult
from ..agents.testing import run_test_agent
from ..core.models import SourceKind, Ticket
from ..core.states import State
from ..forge.auth import _resolve_remote_url, github_token
from ..pass_runner import load_memory, persist_memory
from ..vcs import git_ops
from .base import Outcome, Stage, StageContext
from .pause import (
    check_for_pause,
    save_conversation_state,
    load_conversation_state,
    build_resume_message_history,
    acknowledge_unanswered_threads,
)

log = logging.getLogger("robotsix_mill.stages.implement")

# --- binary-artifact detection --------------------------------------------

BINARY_ARTIFACT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".db",
        ".sqlite",
        ".sqlite3",
        ".pyc",
        ".so",
        ".dylib",
        ".dll",
        ".o",
        ".a",
        ".bin",
        ".exe",
    }
)


def _is_binary_artifact(repo_dir: Path, path: str, target_branch: str) -> bool:
    """Return True if *path* is a binary artifact.

    Uses two orthogonal signals; either is sufficient:

    1. **Extension-based**: the path suffix matches a known binary
       extension (``.db``, ``.pyc``, ``.so``, …).
    2. **Git-based**: ``git diff --numstat origin/<target> -- <path>``
       returns ``-\t-\t<path>`` — the canonical binary marker.
    """
    # Extension-based check (fast path).
    suffix = Path(path).suffix.lower()
    if suffix in BINARY_ARTIFACT_EXTENSIONS:
        return True

    # Git-based check for misnamed binaries.
    try:
        numstat = subprocess.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "diff",
                "--numstat",
                f"origin/{target_branch}",
                "--",
                path,
            ],
            capture_output=True,
            text=True,
        ).stdout.strip()
        if numstat:
            parts = numstat.split("\t")
            if len(parts) >= 2 and parts[0] == "-" and parts[1] == "-":
                return True
    except subprocess.CalledProcessError:
        pass

    return False


# ---------------------------------------------------------------------------
# Internal dataclasses for the refactored implement loop
# ---------------------------------------------------------------------------


@dataclass
class _ImplementContext:
    """Artifact bundle loaded once before the fix loop starts."""

    spec: str
    memory_text: str
    reference_files: list | None
    file_map: set[str] | None
    feedback: str | None
    previous_attempt_summary: str | None
    open_thread_ids: set[int] | None = None


@dataclass
class _ScopeGuardrailResult:
    """Returned by :meth:`_run_scope_guardrail`."""

    action: Literal["continue", "skip_iteration", "return"]
    outcome: Outcome | None = None
    file_map: set[str] | None = None
    feedback: str | None = None


@dataclass
class _SinglePassResult:
    """Returned by :meth:`_run_single_implement_pass`."""

    next_action: Literal["proceed", "retry", "escalate", "return", "pause", "skip"]
    outcome: Outcome | None = None
    feedback: str | None = None
    ic: _ImplementContext | None = None


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------


class ImplementStage(Stage):
    """Clone the repo, create a feature branch, and run the implementation agent loop to produce code changes."""

    name = "implement"
    input_state = State.READY

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        s = ctx.settings

        # --- dependency gate: refuse to implement until all deps are
        # terminal (CLOSED/DONE). Same-state no-op → the reconcile
        # sweep re-enqueues this ticket each poll cycle.
        unmet = ctx.service.unmet_dependencies(ticket)
        if unmet:
            log.debug(
                "%s: unmet dependencies — deferring implement: %s",
                ticket.id,
                unmet,
            )
            return Outcome(State.READY)

        # --- meta-board new-repo extraction gate ---
        if ticket.source == SourceKind.META:
            params = ImplementStage._parse_new_repo_params_for_implement(ctx, ticket)
            if params is not None:
                return ImplementStage._run_repo_scaffold(ctx, ticket, s, params)

        # --- meta-board cross-repo implement gate ---
        # A meta ticket that isn't a new-repo scaffold needs edits across the
        # triaged repos. Run the same triage→clone flow refine uses, then
        # branch the first clone and dive into the standard implement loop
        # with extra_roots threaded through so the agent can read/write
        # across all cloned repos. Per-repo branching and multi-repo PR
        # delivery are sibling children in the same epic — out of scope
        # here.
        extra_roots: list[Path] | None = None
        if ticket.board_id == "meta":
            from ..meta_workspace import build_triaged_meta_workspace

            ws = ctx.service.workspace(ticket)
            spec = ws.read_description()
            repo_dir, extra_roots, outcome = build_triaged_meta_workspace(
                ctx, ticket, ws, spec, author="implement"
            )
            if outcome is not None:
                return outcome
            branch = f"{s.branch_prefix}{ticket.id}"
            # Resume semantics: a prior implement pass may have committed
            # WIP on this branch in this clone. Checkout instead of
            # create_branch when that's the case. No rebase: the clone is
            # fresh from forge_target_branch by construction.
            if git_ops.branch_exists(repo_dir, branch):
                git_ops.checkout(repo_dir, branch)
                resuming = True
            else:
                git_ops.create_branch(repo_dir, branch)
                resuming = False
            ctx.service.set_branch(ticket.id, branch)
        else:
            remote_url = _resolve_remote_url(s, ctx.repo_config)
            if not remote_url:
                return Outcome(State.BLOCKED, "FORGE_REMOTE_URL not configured")

            # Phase 1: clone and branch (or resume)
            result = ImplementStage._clone_and_branch(ctx, ticket, s)
            if isinstance(result, Outcome):
                return result
            repo_dir, branch, resuming = result

        # --- test-baseline check: detect pre-existing failures BEFORE
        # the agent loop so we don't waste cycles on an unfixable base.
        baseline_outcome = ImplementStage._run_baseline_check(
            ctx,
            ticket,
            repo_dir,
            branch,
            resuming,
            s,
        )
        if baseline_outcome is not None:
            return baseline_outcome

        # Phase 2: deterministic, stage-owned implement loop.
        return ImplementStage._implement_loop(
            ctx, ticket, repo_dir, branch, resuming, s, extra_roots=extra_roots
        )

    # ------------------------------------------------------------------
    # Private helpers (refactored)
    # ------------------------------------------------------------------

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
    def _run_scope_guardrail(
        ctx: StageContext,
        ticket: Ticket,
        repo_dir: Path,
        branch: str,
        summary: str,
        ref_files: list[str] | None,
        file_map: set[str] | None,
        settings,
        spec: str,
        current_feedback: str | None,
    ) -> _ScopeGuardrailResult:
        """Check every changed file against the ticket's file_map.

        When ``scope_triage_enabled`` is True an LLM classifier
        decides whether out-of-scope changes are legitimate expansions,
        scope creep (REJECT), or ambiguous (ESCALATE).  Otherwise any
        out-of-scope file immediately blocks the ticket.
        """
        if not file_map:
            return _ScopeGuardrailResult(
                action="skip_iteration",
                file_map=file_map,
                feedback=current_feedback,
            )

        changed = git_ops.changed_files(repo_dir, settings.forge_target_branch)
        out_of_scope = [f for f in changed if f not in file_map]
        if not out_of_scope:
            log.info(
                "%s: scope check passed — %d file(s) changed, "
                "all in file_map (%d allowed)",
                ticket.id,
                len(changed),
                len(file_map),
            )
            return _ScopeGuardrailResult(
                action="skip_iteration",
                file_map=file_map,
                feedback=current_feedback,
            )

        log.warning(
            "%s: scope violation — %d out-of-scope file(s): %s",
            ticket.id,
            len(out_of_scope),
            ", ".join(out_of_scope),
        )

        # --- binary-artifact auto-cleanup ---
        binary_artifacts: list[str] = []
        text_out_of_scope: list[str] = []
        for f in out_of_scope:
            (
                binary_artifacts
                if _is_binary_artifact(repo_dir, f, settings.forge_target_branch)
                else text_out_of_scope
            ).append(f)

        if binary_artifacts:
            cleaned: list[str] = []
            for path in binary_artifacts:
                # Restore tracked version first (no-op for untracked).
                try:
                    subprocess.run(
                        ["git", "-C", str(repo_dir), "checkout", "--", path],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                except subprocess.CalledProcessError:
                    pass
                # If the file still exists on disk, it was untracked
                # — remove it.
                file_path = repo_dir / path
                try:
                    if file_path.exists():
                        file_path.unlink()
                except OSError:
                    log.warning(
                        "%s: failed to unlink binary artifact: %s",
                        ticket.id,
                        path,
                        exc_info=True,
                    )
                log.warning(
                    "%s: auto-cleaned binary artifact: %s",
                    ticket.id,
                    path,
                )
                cleaned.append(path)

            ctx.service.add_step_event(
                ticket.id,
                "scope-triage auto-REJECT (binary artifacts): removed "
                + ", ".join(f"`{f}`" for f in cleaned)
                + " — runtime artifacts, not real work",
            )

        if not text_out_of_scope:
            log.info(
                "%s: all out-of-scope files were binary artifacts — "
                "skipping scope-triage LLM call",
                ticket.id,
            )
            return _ScopeGuardrailResult(
                action="skip_iteration",
                file_map=file_map,
                feedback=current_feedback,
            )

        out_of_scope = text_out_of_scope

        if not settings.scope_triage_enabled:
            ImplementStage._finalize(
                ctx,
                ticket,
                repo_dir,
                branch,
                summary,
                ok=False,
                reference_files=ref_files,
            )
            return _ScopeGuardrailResult(
                action="return",
                outcome=Outcome(
                    State.BLOCKED,
                    f"scope violation: {len(out_of_scope)} file(s) "
                    f"outside ticket scope — "
                    f"{', '.join(out_of_scope)}",
                ),
            )

        # --- scope-triage enabled path ---
        diff_summaries: dict[str, str] = {}
        for path in out_of_scope:
            raw = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_dir),
                    "diff",
                    f"origin/{settings.forge_target_branch}",
                    "--",
                    path,
                ],
                capture_output=True,
                text=True,
            ).stdout
            lines = raw.split("\n")
            diff_summaries[path] = "\n".join(lines[:40])

        from robotsix_mill.agents import scope_triage as st

        try:
            verdict = st.run_scope_triage_agent(
                settings=settings,
                ticket_spec=spec,
                file_map=sorted(file_map),
                out_of_scope_files=out_of_scope,
                diff_summaries=diff_summaries,
            )
        except Exception as exc:
            log.error("%s: scope-triage agent failed: %s", ticket.id, exc)
            verdict = None  # fall through to ESCALATE

        if verdict is not None and verdict.action == "EXPAND":
            new_files = [f for f in verdict.expand_files if f not in file_map]
            if not new_files:
                log.info(
                    "%s: scope-triage EXPAND — all %d file(s) already in file_map; skipping",
                    ticket.id,
                    len(verdict.expand_files),
                )
                return _ScopeGuardrailResult(
                    action="skip_iteration",
                    file_map=file_map,
                    feedback=None,
                )
            for f in new_files:
                file_map.add(f)
            log.info(
                "%s: scope-triage EXPAND — %s",
                ticket.id,
                verdict.justification,
            )
            # Pre-v1 this was an add_comment; agent conclusions now
            # live in history, comments are reserved for ASK_USER +
            # review threads. The implement state doesn't change
            # here (the loop continues), so this is a same-state
            # step event.
            ctx.service.add_step_event(
                ticket.id,
                f"scope-triage EXPAND: {verdict.justification} "
                f"(added: {', '.join(new_files)})",
            )
            # Retroactive short-circuit: when every expand-file was
            # already modified in this pass, fall through to the test
            # gate instead of re-running the agent.
            if set(new_files).issubset(set(changed)):
                log.info(
                    "%s: scope-triage EXPAND retroactive — "
                    "all expanded files already modified; "
                    "skipping agent re-run",
                    ticket.id,
                )
                return _ScopeGuardrailResult(
                    action="skip_iteration",
                    file_map=file_map,
                    feedback=None,
                )
            else:
                return _ScopeGuardrailResult(
                    action="continue",
                    file_map=file_map,
                    feedback=None,
                )

        if verdict is not None and verdict.action == "REJECT":
            # Dedup guard: if ALL current out-of-scope files were
            # already REJECTed by a prior scope-triage step on this
            # ticket, the agent has seen this diff before and the
            # operator already has the signal.  Don't emit another
            # event / bounce back to READY — treat as implicit
            # EXPAND so the implement loop can make actual progress.
            # Pre-v1 this read prior REJECT *comments*; now reads
            # prior REJECT *history events* since scope-triage is no
            # longer a commenter.
            prior_rejects = [
                ev
                for ev in ctx.service.history(ticket.id)
                if ev.note and ev.note.startswith("scope-triage REJECT")
            ]
            already_rejected: set[str] = set()
            for ev in prior_rejects:
                for m in _re.findall(r"`([^`]+)`", ev.note or ""):
                    already_rejected.add(m)
            new_oos = [f for f in out_of_scope if f not in already_rejected]
            if not new_oos:
                log.warning(
                    "%s: suppressing duplicate scope-triage REJECT — "
                    "all %d out-of-scope file(s) already rejected in "
                    "prior run(s): %s",
                    ticket.id,
                    len(out_of_scope),
                    ", ".join(out_of_scope),
                )
                for f in out_of_scope:
                    file_map.add(f)
                return _ScopeGuardrailResult(
                    action="skip_iteration",
                    file_map=file_map,
                    feedback=None,
                )

            log.info(
                "%s: scope-triage REJECT — %s",
                ticket.id,
                verdict.justification,
            )
            ImplementStage._finalize(
                ctx,
                ticket,
                repo_dir,
                branch,
                summary,
                ok=False,
                reference_files=ref_files,
            )
            # Files listed in backticks so the same-pattern dedup
            # loop (line ~340) keeps working when this REJECT event
            # is re-scanned next pass.
            file_list = ", ".join(f"`{f}`" for f in out_of_scope)
            return _ScopeGuardrailResult(
                action="return",
                outcome=Outcome(
                    State.READY,
                    f"scope-triage REJECT: {verdict.justification[:200]} "
                    f"— out-of-scope: {file_list}",
                ),
            )

        # ESCALATE (or agent error fall-through).
        reason = (
            f"scope-triage ESCALATE: {verdict.justification}"
            if verdict is not None
            else "scope-triage agent error — escalated for human review"
        )
        log.warning("%s: %s", ticket.id, reason)
        ImplementStage._finalize(
            ctx,
            ticket,
            repo_dir,
            branch,
            summary,
            ok=False,
            reference_files=ref_files,
        )
        file_list = ", ".join(f"`{f}`" for f in out_of_scope)
        # The reason becomes the transition note; the out-of-scope
        # file list is included so operators see what triggered the
        # escalation without digging into artifacts.
        return _ScopeGuardrailResult(
            action="return",
            outcome=Outcome(
                State.BLOCKED,
                f"{reason} — out-of-scope: {file_list}",
            ),
        )

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

        # Resolve per-repo language instructions for the implement agent.
        language_instructions = ""
        if ctx.repo_config and ctx.repo_config.language:
            lang = ctx.repo_config.language
            snippet_path = settings.language_instructions_dir / f"{lang}.md"
            try:
                language_instructions = snippet_path.read_text(encoding="utf-8")
            except OSError:
                log.info(
                    "%s: language '%s' configured but no snippet at %s — "
                    "skipping language instructions",
                    ticket.id,
                    lang,
                    snippet_path,
                )

        # --- agent invocation ---
        # Gating heuristic: when the previous attempt already concluded
        # ``no_change_needed`` (the summary or feedback carries that
        # signal), the retry is a pure re-check — use the cheaper
        # ``no_change_model`` instead of the primary model.
        agent_model: str | None = None
        _prev = (ic.previous_attempt_summary or "") + (ic.feedback or "")
        if "no change needed" in _prev.lower():
            agent_model = settings.no_change_model

        try:
            (
                summary,
                ref_files,
                updated_memory,
                conv_state,
                new_msgs,
                no_change_needed,
                no_change_rationale,
            ) = coding.run_implement_agent(
                settings=settings,
                repo_dir=repo_dir,
                spec=ic.spec,
                feedback=ic.feedback,
                memory=ic.memory_text,
                reference_files=ic.reference_files,
                previous_attempt_summary=ic.previous_attempt_summary,
                file_map=ic.file_map,
                board_id=memory_board_id,
                message_history=resume_history,
                language_instructions=language_instructions,
                extra_roots=extra_roots,
                model_name=agent_model,
            )
        except AgentBudgetError as e:
            ImplementStage._finalize(
                ctx,
                ticket,
                repo_dir,
                branch,
                f"budget cap hit: {e}",
                ok=False,
            )
            return _SinglePassResult(
                next_action="return",
                outcome=Outcome(
                    State.BLOCKED,
                    f"agent budget cap — resumable (move to READY): {e}",
                ),
            )
        except AgentRunError as e:
            ImplementStage._finalize(
                ctx,
                ticket,
                repo_dir,
                branch,
                f"agent error: {e}",
                ok=False,
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
                from ..runtime.transient_errors import classify_stage_error

                if classify_stage_error(e.cause) == "transient":
                    raise e.cause
            return _SinglePassResult(
                next_action="return",
                outcome=Outcome(
                    State.BLOCKED,
                    f"agent error — resumable: {e}",
                ),
            )

        # --- pause detection ---
        if check_for_pause(new_msgs):
            save_conversation_state(ws, conv_state, "implement")
            ImplementStage._finalize(
                ctx,
                ticket,
                repo_dir,
                branch,
                summary or "paused",
                ok=False,
                reference_files=ref_files,
            )
            ctx.service.transition(
                ticket.id,
                State.AWAITING_USER_REPLY,
                note="paused — agent asked a clarifying question",
            )
            log.info(
                "%s: paused implement — agent invoked ask_user",
                ticket.id,
            )
            return _SinglePassResult(
                next_action="pause",
                outcome=Outcome(State.AWAITING_USER_REPLY),
            )

        # --- persistence ---
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

        # --- scope guardrail ---
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

        # Determine the updated file_map and feedback after the guardrail.
        new_file_map = (
            guardrail.file_map if guardrail.file_map is not None else ic.file_map
        )
        new_feedback = (
            guardrail.feedback
            if guardrail.action in ("continue", "skip_iteration")
            else ic.feedback
        )

        # Build updated context for potential retry.
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

        # --- test gate ---
        passed, diag = run_test_agent(
            settings=settings,
            repo_dir=repo_dir,
            repo_config=ctx.repo_config,
        )
        if not passed and diag.startswith("sandbox unavailable"):
            ImplementStage._finalize(
                ctx,
                ticket,
                repo_dir,
                branch,
                summary,
                ok=False,
                reference_files=ref_files,
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
                not git_ops.has_changes(repo_dir)
                and not git_ops.branch_is_ahead_of_main(repo_dir)
                and no_change_needed
                and no_change_rationale.strip()
            ):
                rationale = no_change_rationale.strip()
                short = rationale[:400] + ("…" if len(rationale) > 400 else "")
                ImplementStage._finalize(
                    ctx,
                    ticket,
                    repo_dir,
                    branch,
                    f"no change needed — {rationale}",
                    ok=True,
                    reference_files=ref_files,
                )
                return _SinglePassResult(
                    next_action="return",
                    outcome=Outcome(State.DONE, f"no change needed — {short}"),
                )
            if not git_ops.has_changes(repo_dir) and not resuming:
                # Silent no-change on a fresh run (agent didn't signal):
                # BLOCK so the operator can investigate. Capture the
                # agent's own narrative so they have something to
                # inspect — otherwise the ticket lands in BLOCKED with
                # only a one-line reason and the previous iteration's
                # artifacts (which may not exist on a fresh implement
                # run).
                no_change_summary = summary or (
                    "Agent finished without producing any file edits and "
                    "without explanation. Check artifacts/implement_messages.json "
                    "for the full transcript."
                )
                ImplementStage._finalize(
                    ctx,
                    ticket,
                    repo_dir,
                    branch,
                    f"{no_change_summary}\n\n"
                    "[Diagnostic] implement returned BLOCKED because "
                    "`git diff` was empty after the agent run AND the "
                    "agent did NOT set ``no_change_needed=True``. "
                    "Common causes: (1) agent decided no edits were "
                    "necessary but didn't escalate via the result "
                    "schema; (2) the agent loaded a stale "
                    "conversation_state from a sibling stage and "
                    "treated it as already-completed work.",
                    ok=False,
                    reference_files=ref_files,
                )
                return _SinglePassResult(
                    next_action="return",
                    outcome=Outcome(State.BLOCKED, "no changes produced"),
                )
            # --- post-agent thread acknowledgment ---
            if ic.open_thread_ids and ic.feedback:
                acknowledge_unanswered_threads(ctx, ticket, ic.open_thread_ids)
            ImplementStage._finalize(
                ctx,
                ticket,
                repo_dir,
                branch,
                summary,
                ok=True,
                reference_files=ref_files,
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
            return _SinglePassResult(
                next_action="proceed",
                outcome=Outcome(next_state, next_note),
            )

        if decision.next_action == "escalate":
            ImplementStage._finalize(
                ctx,
                ticket,
                repo_dir,
                branch,
                summary,
                ok=False,
                reference_files=ref_files,
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
        )

    # ------------------------------------------------------------------
    # test-baseline check
    # ------------------------------------------------------------------

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

        # Resolve the current base-branch SHA.
        base_sha = git_ops.remote_branch_sha(repo_dir, settings.forge_target_branch)
        if base_sha is None:
            base_sha = git_ops.head_sha(repo_dir)

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
                    return Outcome(
                        State.BLOCKED,
                        f"pre-existing test failures on {settings.forge_target_branch} "
                        f"({base_sha[:8]}): {diag[:400]}",
                    )
                if cached_passed:
                    # Base advanced but cached result was passing — a
                    # passing baseline stays valid (AC7).
                    return None
                # Base advanced AND cached result was failing → re-run
                # (operator may have fixed the branch between retries).

        # --- execute baseline check ---
        git_ops.checkout(repo_dir, settings.forge_target_branch)
        try:
            passed, diag = run_test_agent(
                settings=settings,
                repo_dir=repo_dir,
                repo_config=ctx.repo_config,
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
        )
        return Outcome(
            State.BLOCKED,
            f"pre-existing test failures on {settings.forge_target_branch} "
            f"({base_sha[:8]}): {diag[:400]}",
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

    @staticmethod
    def _implement_loop(
        ctx,
        ticket,
        repo_dir,
        branch,
        resuming,
        settings,
        extra_roots: list[Path] | None = None,
    ):
        """Run the bounded fix loop: edit pass → test gate → route.

        The implement agent does ONE edit pass per iteration; the test
        gate runs the suite once and produces a distilled diagnosis;
        :meth:`ValidationResult.decide` routes deterministically. On
        ``retry`` the diagnosis is fed back into the next pass; on
        ``escalate`` (suite still failing after ``max_fix_iterations``)
        the ticket is BLOCKED-resumable. No LLM owns the loop or the
        bound — both are enforced here.
        """
        max_iters = max(1, settings.max_fix_iterations)
        ic = ImplementStage._load_implement_context(ctx, ticket, settings)

        for attempt in range(1, max_iters + 1):
            # --- resume awareness: detect if returning from a pause ---
            resume_history: list | None = None
            if attempt == 1:
                ws = ctx.service.workspace(ticket)
                saved_state = load_conversation_state(ws, "implement")
                if saved_state is not None and any(
                    ev.state == State.AWAITING_USER_REPLY
                    for ev in ctx.service.history(ticket.id)
                ):
                    from .pause import _collect_ask_user_replies

                    reply_text = _collect_ask_user_replies(ctx, ticket)
                    resume_history = build_resume_message_history(
                        saved_state,
                        reply_text,
                    )
                    log.info(
                        "%s: resuming implement from pause — "
                        "loaded %d-byte conversation state",
                        ticket.id,
                        len(saved_state),
                    )
                    ic.feedback = None

            result = ImplementStage._run_single_implement_pass(
                ctx,
                ticket,
                repo_dir,
                branch,
                settings,
                ic,
                attempt,
                max_iters,
                resume_history,
                resuming,
                extra_roots=extra_roots,
            )

            if result.next_action == "return":
                return result.outcome
            if result.next_action == "pause":
                return result.outcome
            if result.next_action in ("proceed", "escalate"):
                return result.outcome

            # next_action == "retry" — update for next iteration.
            if result.ic is not None:
                ic = result.ic

        # Defensive fallback — should be unreachable.
        ImplementStage._finalize(
            ctx,
            ticket,
            repo_dir,
            branch,
            "",
            ok=False,
            reference_files=ic.reference_files,
        )
        return Outcome(
            State.BLOCKED,
            "implement loop exhausted — resumable",
        )

    # ------------------------------------------------------------------
    # Existing helpers (NOT refactored)
    # ------------------------------------------------------------------

    @staticmethod
    def _finalize(
        ctx,
        ticket,
        repo_dir,
        branch,
        summary,
        *,
        ok: bool,
        reference_files: list[str] | None = None,
    ) -> None:
        ws = ctx.service.workspace(ticket)
        (ws.artifacts_dir / "implement.md").write_text(
            f"# Implement ({'passed' if ok else 'BLOCKED — resumable'})\n"
            f"branch: {branch}\n\n{summary}\n",
            encoding="utf-8",
        )
        # Persist agent-curated reference_files (paths-only) for retry
        # pre-seeding. Overwrite refine's version unconditionally.
        try:
            ref_path = ws.artifacts_dir / "reference_files.json"
            ref_path.write_text(
                json.dumps(
                    [{"path": p} for p in (reference_files or [])],
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            log.warning(
                "%s: failed to write reference_files.json",
                ticket.id,
                exc_info=True,
            )
        # Persist the summary as a standalone artifact for
        # `<previous_attempt>` injection on retry.
        try:
            (ws.artifacts_dir / "implement_summary.md").write_text(
                summary,
                encoding="utf-8",
            )
        except OSError:
            log.warning(
                "%s: failed to write implement_summary.md",
                ticket.id,
                exc_info=True,
            )
        if git_ops.has_changes(repo_dir):
            git_ops.commit_all(
                repo_dir,
                f"mill: {ticket.title} ({ticket.id})" + ("" if ok else " [WIP]"),
            )

    @staticmethod
    def _clone_and_branch(ctx, ticket, settings):
        ws = ctx.service.workspace(ticket)
        repo_dir = ws.dir / "repo"
        branch = f"{settings.branch_prefix}{ticket.id}"
        remote_url = _resolve_remote_url(settings, ctx.repo_config)

        # Resume iff a prior run left this ticket's clone + branch behind.
        resuming = (repo_dir / ".git").exists() and git_ops.branch_exists(
            repo_dir, branch
        )
        if resuming:
            git_ops.checkout(repo_dir, branch)
        else:
            if repo_dir.exists():
                shutil.rmtree(repo_dir)
            try:
                try:
                    token = github_token(settings, repo_config=ctx.repo_config)
                except RuntimeError:
                    token = None
                git_ops.clone(
                    remote_url,
                    repo_dir,
                    settings.forge_target_branch,
                    token,
                )
            except subprocess.CalledProcessError as e:
                return Outcome(State.BLOCKED, f"clone failed: {e.stderr[:300]}")
            git_ops.create_branch(repo_dir, branch)

        # Refresh against current origin/<target> so the agent never
        # edits stale source — a branch based on even slightly outdated
        # origin/<target> can silently revert newer commits.
        # Pass a freshly minted token so try_rebase_onto's fetch
        # doesn't fall back to origin's stored (and likely expired)
        # GitHub App token — see git_ops.try_rebase_onto for the full
        # rationale. Token resolution can raise when the forge is
        # unconfigured (tests, file:// remotes); fall back to no token
        # and let try_rebase_onto use origin as-is.
        try:
            _rebase_token = github_token(
                settings,
                repo_config=ctx.repo_config,
            )
        except Exception:
            _rebase_token = None
        if not git_ops.try_rebase_onto(
            repo_dir,
            settings.forge_target_branch,
            remote_url=_resolve_remote_url(settings, ctx.repo_config),
            token=_rebase_token,
        ):
            return Outcome(
                State.REBASING,
                f"rebase onto origin/{settings.forge_target_branch} "
                "failed — handing to rebase agent",
            )

        # Hard invariant: NEVER run the agent / sandbox without a
        # materialized clone.
        if not (repo_dir / ".git").exists():
            log.warning(
                "%s: clone missing before agent run — re-cloning",
                ticket.id,
            )
            if repo_dir.exists():
                shutil.rmtree(repo_dir, ignore_errors=True)
            try:
                try:
                    token = github_token(settings, repo_config=ctx.repo_config)
                except RuntimeError:
                    token = None
                git_ops.clone(
                    remote_url,
                    repo_dir,
                    settings.forge_target_branch,
                    token,
                )
                git_ops.create_branch(repo_dir, branch)
            except subprocess.CalledProcessError as e:
                return Outcome(
                    State.BLOCKED,
                    "repo clone missing and re-clone failed — "
                    f"resumable: {(e.stderr or '')[:200]}",
                )
        ctx.service.set_branch(ticket.id, branch)
        return (repo_dir, branch, resuming)

    # ------------------------------------------------------------------
    # meta-board new-repo extraction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_new_repo_params_for_implement(
        ctx: StageContext, ticket: Ticket
    ) -> dict | None:
        """Thin wrapper that reads the ticket description and calls
        :func:`~robotsix_mill.repo_scaffold.parse_new_repo_params`.

        Lazy-imports the repo_scaffold module to avoid import-time
        coupling between the implement stage and the scaffold workflow.
        """
        from ..repo_scaffold import parse_new_repo_params

        description = ctx.service.workspace(ticket).read_description()
        return parse_new_repo_params(description)

    @staticmethod
    def _run_repo_scaffold(
        ctx: StageContext,
        ticket: Ticket,
        s,
        params: dict,
    ) -> Outcome:
        """Resolve the forge, call the scaffold workflow, and return its Outcome."""
        from ..forge.base import get_forge
        from ..repo_scaffold import run_repo_scaffold

        forge = get_forge(s, repo_config=None)
        return run_repo_scaffold(s, ticket, forge, ctx)
