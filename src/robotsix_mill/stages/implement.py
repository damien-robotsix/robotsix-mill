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
from ..agents import prerequisite
from ..agents.coding import AgentBudgetError, AgentRunError
from ..agents.coordinating import ValidationResult
from ..agents.testing import (
    ENV_ERROR_PREFIX,
    run_smoke_agent,
    run_test_agent,
    smoke_paths_match,
)
from ..core.models import SourceKind, Ticket
from ..core.states import State
from ..forge.auth import _resolve_remote_url, github_token
from ..repo_settings import load_repo_smoke_command, load_repo_smoke_paths
from ..runners.pass_runner import load_memory, persist_memory
from ..vcs import git_ops
from .base import Outcome, Stage, StageContext
from . import dependency_fix
from . import short_circuit_verify
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


@dataclass
class _AgentRunOutcome:
    """Result of the agent invocation phase.

    Exactly one of ``success`` / ``failure`` is non-None.  ``success``
    holds the 7-tuple returned by ``coding.run_implement_agent``
    (summary, ref_files, updated_memory, conv_state, new_msgs,
    no_change_needed, no_change_rationale); ``failure`` holds the
    ``_SinglePassResult`` the orchestrator should return when the agent
    call raised a caught error.  Used only inside ``implement.py`` to
    let the orchestrator early-return cleanly without leaking the
    dual-path complexity.
    """

    success: tuple | None = None
    failure: _SinglePassResult | None = None


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------


class ImplementStage(Stage):
    """Clone the repo, create a feature branch, and run the implementation agent loop to produce code changes."""

    name = "implement"
    input_state = State.READY

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Process a READY ticket: gate on dependencies, clone the repo, create the feature branch, and drive the implementation agent loop to produce code changes."""
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
            from ..meta.workspace import build_triaged_meta_workspace

            ws = ctx.service.workspace(ticket)
            spec = ws.read_description()
            repo_dir, extra_roots, outcome = build_triaged_meta_workspace(
                ctx, ticket, ws, spec, author="implement"
            )
            if outcome is not None:
                return outcome
            branch = f"{s.branch_prefix}{ticket.id}"
            # Create/checkout the feature branch in EVERY workspace
            # repo, not just the first clone. Deliver needs per-repo
            # branches to open one PR per touched repo.
            for repo_path in extra_roots:
                if git_ops.branch_exists(repo_path, branch):
                    git_ops.checkout(repo_path, branch)
                else:
                    git_ops.create_branch(repo_path, branch)
            # resuming is true iff the primary repo already had the
            # branch from a prior implement pass.
            resuming = git_ops.branch_exists(repo_dir, branch)
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

        # --- prepare hook: let the repo run custom setup after clone,
        # before any agent executes ---
        ws = ctx.service.workspace(ticket)
        from ..hooks import run_prepare_hook

        hook_error = run_prepare_hook(repo_dir, ticket.id, ws.dir)
        if hook_error is not None:
            return Outcome(State.BLOCKED, hook_error)

        # --- prerequisite gate: cheapest pre-agent check, so it runs
        # first. Verify that external symbol/import prerequisites the
        # spec declares are satisfiable in the cloned repo's environment
        # BEFORE spending the baseline run or the coordinator agent.
        prereq_outcome = ImplementStage._run_prerequisite_gate(
            ctx,
            ticket,
            ctx.service.workspace(ticket).read_description(),
            repo_dir,
            s,
        )
        if prereq_outcome is not None:
            return prereq_outcome

        # --- test-baseline check: detect pre-existing failures BEFORE
        # the agent loop so we don't waste cycles on an unfixable base.
        # EXEMPT baseline-fix tickets: a ticket spawned to repair the red
        # base (source=IMPLEMENT_BASELINE_DEPENDENCY) must implement AGAINST
        # that still-red base — that is its whole job. Re-running the gate
        # on it would spawn yet another baseline fix, which dedups to the
        # ticket itself ("Ticket cannot depend on itself" → Fatal), wedging
        # the ticket and everything parked behind it (board-wide deadlock).
        if ticket.source != SourceKind.IMPLEMENT_BASELINE_DEPENDENCY:
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

        changed = git_ops.introduced_files(repo_dir, settings.forge_target_branch)
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
                extra_roots=None,
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
            if not raw.strip():
                # NEW (untracked) files produce an EMPTY ``git diff`` — the
                # triage agent then sees "no visible content", cannot judge
                # the file, and ESCALATEs to a human (live case: the
                # worker.py package refactor cb63, whose new submodules all
                # summarized empty). Show the file head instead so the
                # agent gets the same 40-line budget of real content.
                file_path = repo_dir / path
                if file_path.is_file():
                    try:
                        head = file_path.read_text(
                            encoding="utf-8", errors="replace"
                        ).split("\n")[:38]
                        raw = "NEW FILE (untracked — no diff vs base):\n" + "\n".join(
                            head
                        )
                    except OSError:
                        raw = "NEW FILE (untracked — unreadable)"
            lines = raw.split("\n")
            diff_summaries[path] = "\n".join(lines[:40])

        from robotsix_mill.agents import scope_triage as st

        triage_error: str | None = None
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
            # Keep the WHAT for the operator-visible note — a bare
            # "agent error" reads like a scope verdict and sends the
            # human hunting through logs for a transient model failure.
            triage_error = f"{type(exc).__name__}: {exc}"
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
                # The agent re-created files a prior REJECT already
                # cleaned. Don't bounce to READY again (that ping-pongs
                # forever) — but DON'T add them to file_map either, which
                # used to silently ship previously-REJECTed scope creep.
                # Clean them out of the tree again and fall through to the
                # test gate so the in-scope work can still make progress.
                log.warning(
                    "%s: duplicate scope-triage REJECT — all %d out-of-scope "
                    "file(s) re-created after a prior REJECT cleanup: %s. "
                    "Removing them again; not shipping without an explicit "
                    "EXPAND verdict.",
                    ticket.id,
                    len(out_of_scope),
                    ", ".join(out_of_scope),
                )
                git_ops.restore_paths(
                    repo_dir, settings.forge_target_branch, out_of_scope
                )
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
            # Remove the rejected out-of-scope changes from the working
            # tree BEFORE finalize commits, so the WIP commit (and every
            # resumed run off it) starts from the spec'd scope only.
            # Handles both unstaged and already-WIP-committed pollution.
            git_ops.restore_paths(repo_dir, settings.forge_target_branch, out_of_scope)
            ImplementStage._finalize(
                ctx,
                ticket,
                repo_dir,
                branch,
                summary,
                ok=False,
                reference_files=ref_files,
                extra_roots=None,
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
            else (
                f"scope-triage agent error ({(triage_error or 'unknown')[:160]}) "
                "— escalated for human review; resume-blocked re-runs the triage"
            )
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
            extra_roots=None,
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
    def _resolve_language_instructions(
        ctx: StageContext, ticket: Ticket, settings
    ) -> str:
        """Resolve the concatenated per-language instruction block, or
        ``""``. The repo's own ``.robotsix-mill/config.yaml`` ``languages``
        declaration (+ optional ``.robotsix-mill/language_instructions/``
        overrides) win over the central ``repos.yaml`` ``language``."""
        from ..repo_settings import resolve_language_instructions

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
                from ..runtime.transient_errors import classify_stage_error

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
    def _maybe_handle_pause(
        ctx: StageContext,
        ticket: Ticket,
        repo_dir: Path,
        branch: str,
        ws,
        summary: str,
        ref_files: list[str] | None,
        conv_state,
        new_msgs,
        extra_roots: list[Path] | None,
    ) -> _SinglePassResult | None:
        """Persist conv_state and route to AWAITING_USER_REPLY on pause."""
        if not check_for_pause(new_msgs):
            return None
        save_conversation_state(ws, conv_state, "implement")
        ImplementStage._finalize(
            ctx,
            ticket,
            repo_dir,
            branch,
            summary or "paused",
            ok=False,
            reference_files=ref_files,
            extra_roots=extra_roots,
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

    @staticmethod
    def _persist_pass_artifacts(
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

    @staticmethod
    def _evaluate_test_results(
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
        passed, diag = run_test_agent(
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
                changed = git_ops.introduced_files(
                    repo_dir, settings.forge_target_branch
                )
                smoke_paths = load_repo_smoke_paths(repo_dir)
                if smoke_paths_match(changed, smoke_paths):
                    smoke_passed, smoke_diag = run_smoke_agent(
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
                    ws = ctx.service.workspace(ticket)
                    src_png = repo_dir / "artifacts" / "board.png"
                    if src_png.exists():
                        shutil.copyfile(src_png, ws.artifacts_dir / "board.png")
                    if not smoke_passed:
                        passed = False
                        diag = smoke_diag
        if not passed and diag.startswith("sandbox unavailable"):
            ImplementStage._finalize(
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
                not ImplementStage._any_repo_has_changes(repo_dir, extra_roots)
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
                if edit_tools:
                    tool_list = ", ".join(edit_tools)
                    diag = (
                        f"{no_change_rationale.strip() or summary}\n\n"
                        "[Diagnostic] implement was about to close this ticket "
                        "as ``no_change_needed`` because ``git diff`` is empty "
                        f"— but the agent invoked file-mutating tools "
                        f"({tool_list}) during the run. An empty diff after "
                        "real edit calls means the work did NOT persist (edits "
                        "reverted, workspace reset mid-run, or written outside "
                        "the clone). Closing as no-change would silently lose "
                        "that work, so the ticket is BLOCKED for inspection. "
                        "Re-run implement; if the spec genuinely needs no "
                        "change, the agent must reach that conclusion WITHOUT "
                        "calling write_file/edit_file/Write/Edit."
                    )
                    ImplementStage._finalize(
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
                ImplementStage._finalize(
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
            if (
                not ImplementStage._any_repo_has_changes(repo_dir, extra_roots)
                and not resuming
            ):
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
                # Gitignored-edit detector: real writes into a gitignored
                # path (e.g. a manifest board whose ``.gitignore`` carries
                # ``/src/*`` for vcs-imported sub-repos) are invisible to
                # ``git status`` and surface here as an opaque empty diff.
                # Name the paths so the operator sees WHAT happened instead
                # of guessing (live case: robotsix-mill-ros2 writes under
                # ``src/ros2/…`` blocked as "no changes produced").
                ignored_hits = ImplementStage._claimed_gitignored_edits(
                    repo_dir, new_msgs
                )
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
                    extra_roots=extra_roots,
                )
                # Surface the agent's OWN reason in the operator-visible
                # block note — not a bare "no changes produced". Otherwise the
                # ticket lands in BLOCKED with an opaque one-liner and the
                # operator has to open artifacts/implement.md to learn why
                # (the recurring "blocked with no explanation" complaint). The
                # full narrative still lives in implement.md (_finalize above);
                # this is the short, operator-facing form.
                reason = " ".join(no_change_summary.split())
                note = "no changes produced"
                if reason:
                    note = f"no changes produced — {reason[:300]}" + (
                        "… (see implement.md)" if len(reason) > 300 else ""
                    )
                return _SinglePassResult(
                    next_action="return",
                    outcome=Outcome(State.BLOCKED, note),
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
            changed = git_ops.introduced_files(repo_dir, settings.forge_target_branch)
            if extra_roots:
                for repo_path in extra_roots:
                    # Mirror _any_repo_has_changes: the primary repo is
                    # already covered above; skip the duplicate entry.
                    if repo_path == repo_dir:
                        continue
                    changed = list(
                        set(changed)
                        | set(
                            git_ops.introduced_files(
                                repo_path, settings.forge_target_branch
                            )
                        )
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
                    f"origin/{settings.forge_target_branch}. An edit-tool-call "
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
                    )
                # Iterations exhausted → do NOT hand un-landed claims to
                # review. BLOCK for inspection, mirroring the empty-diff
                # contradiction guard's shape.
                ImplementStage._finalize(
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
            ImplementStage._finalize(
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

    # ------------------------------------------------------------------
    # prerequisite gate
    # ------------------------------------------------------------------

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
            passed, diag = run_test_agent(
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

        # Ordered history of the per-cycle distilled diagnosis. Drives the
        # circuit breaker below: a fix loop that keeps producing the SAME
        # diagnosis is not making progress, and an ENV-ERROR diagnosis is
        # not fixable by code edits at all — both should short-circuit to
        # BLOCKED rather than exhaust ``max_fix_iterations``.
        diag_history: list[str] = []

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
            # Circuit breaker: track the per-cycle diagnosis and bail out
            # early when the loop is provably stuck. The retry diagnosis is
            # carried on ``result.feedback`` (main retry path) or, when the
            # guardrail produced a "continue", on ``result.ic.feedback``.
            diag = (
                result.feedback
                or (result.ic.feedback if result.ic is not None else None)
                or ""
            )
            diag_history.append(diag)
            env_repeat = (
                diag.startswith(ENV_ERROR_PREFIX)
                and len(diag_history) >= 2
                and diag_history[-2] == diag
            )
            triple_repeat = (
                len(diag_history) >= 3
                and diag != ""
                and diag_history[-2] == diag
                and diag_history[-3] == diag
            )
            if env_repeat or triple_repeat:
                note = (
                    "environment failure not fixable by code edits — "
                    f"{diag[:200]} (short-circuited after {attempt} "
                    "cycle(s) of identical diagnosis)"
                )
                ImplementStage._finalize(
                    ctx,
                    ticket,
                    repo_dir,
                    branch,
                    note,
                    ok=False,
                    reference_files=ic.reference_files,
                    extra_roots=extra_roots,
                )
                return Outcome(State.BLOCKED, note)
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
            extra_roots=extra_roots,
        )
        return Outcome(
            State.BLOCKED,
            "implement loop exhausted — resumable",
        )

    # ------------------------------------------------------------------
    # Existing helpers (NOT refactored)
    # ------------------------------------------------------------------

    @staticmethod
    def _any_repo_has_changes(repo_dir: Path, extra_roots: list[Path] | None) -> bool:
        """Return True if any repo has uncommitted changes or is ahead of main.

        Used by the two exit-path guards so multi-repo tickets don't
        misroute to DONE/BLOCKED when only the primary repo was checked.
        """
        if git_ops.has_changes(repo_dir) or git_ops.branch_is_ahead_of_main(repo_dir):
            return True
        if extra_roots:
            for repo_path in extra_roots:
                if repo_path == repo_dir:
                    continue
                if git_ops.has_changes(repo_path) or git_ops.branch_is_ahead_of_main(
                    repo_path
                ):
                    return True
        return False

    @staticmethod
    def _claimed_gitignored_edits(
        repo_dir: Path, new_messages: bytes | str | None
    ) -> list[str]:
        """Repo-relative paths this run's edit tool-calls targeted that exist
        on disk but are gitignored (so the diff stays empty).

        Normalizes Claude-SDK absolute paths to repo-relative; paths outside
        the clone are skipped (a different failure mode with its own guard).
        Fail-open: errors yield ``[]`` — this only ENRICHES the blocked note,
        it never decides the outcome.
        """
        try:
            raw_paths = short_circuit_verify.run_claimed_edited_rawpaths(new_messages)
            rels: list[str] = []
            seen: set[str] = set()
            for raw in raw_paths:
                p = Path(raw)
                if p.is_absolute():
                    try:
                        p = p.relative_to(repo_dir)
                    except ValueError:
                        continue  # outside the clone
                rel = str(p)
                # Dedupe AFTER normalization — the same file can be claimed
                # both repo-relative (mill tools) and absolute (Claude SDK).
                if rel not in seen:
                    seen.add(rel)
                    rels.append(rel)
            return git_ops.ignored_existing_paths(repo_dir, rels)
        except Exception:  # noqa: BLE001 — diagnostic enrichment only
            log.warning(
                "gitignored-edit detection failed; emitting plain note",
                exc_info=True,
            )
            return []

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
        extra_roots: list[Path] | None = None,
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
        # Commit message format — identical for all repos.
        commit_message = f"mill: {ticket.title} ({ticket.id})" + (
            "" if ok else " [WIP]"
        )
        # Per-repo commit for extra_roots (multi-repo meta tickets).
        # Write a touched_repos.json artifact listing every repo that
        # received a commit so the downstream deliver stage knows which
        # repos to open PRs for.
        touched_repos: list[dict] = []
        if extra_roots is not None:
            # Check all repos for changes BEFORE committing any, so
            # has_changes returns the correct answer for every repo.
            if git_ops.has_changes(repo_dir):
                touched_repos.append(
                    {
                        "repo_id": repo_dir.name,
                        "branch": branch,
                        "repo_path": str(repo_dir),
                    }
                )
            for repo_path in extra_roots:
                if repo_path == repo_dir:
                    continue
                if git_ops.has_changes(repo_path):
                    touched_repos.append(
                        {
                            "repo_id": repo_path.name,
                            "branch": branch,
                            "repo_path": str(repo_path),
                        }
                    )
        # Commit primary repo (always — regardless of extra_roots).
        if git_ops.has_changes(repo_dir):
            git_ops.commit_all(repo_dir, commit_message)
        # Commit extra repos (skip primary — already done above).
        if extra_roots is not None:
            for repo_path in extra_roots:
                if repo_path == repo_dir:
                    continue
                if git_ops.has_changes(repo_path):
                    git_ops.commit_all(repo_path, commit_message)
            # Write the artifact — even if empty (no-change-needed path).
            try:
                (ws.artifacts_dir / "touched_repos.json").write_text(
                    json.dumps(touched_repos, indent=2),
                    encoding="utf-8",
                )
            except OSError:
                log.warning(
                    "%s: failed to write touched_repos.json",
                    ticket.id,
                    exc_info=True,
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
