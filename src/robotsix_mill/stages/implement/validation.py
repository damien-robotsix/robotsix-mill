"""Scope-guardrail and test-result evaluation for the implement stage.

The two largest implement methods — :meth:`_run_scope_guardrail`
(out-of-scope file triage / binary-artifact cleanup) and
:meth:`_evaluate_test_results` (test + smoke gate, ``ValidationResult``
routing) — live here as :class:`ValidationMixin` to keep the sibling
modules under the line budget.  Mixed into :class:`ImplementStage`
(assembled in ``phase_coordinator``).
"""

from __future__ import annotations

import logging
import re as _re
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from ...agents.coordinating import ValidationResult
from ...agents.testing import smoke_paths_match
from ...core.models import Ticket
from ...core.states import State
from ...repo_settings import load_repo_smoke_command
from ...vcs import git_ops
from .. import short_circuit_verify
from ..base import Outcome, StageContext
from ..pause import acknowledge_unanswered_threads
from .file_operations import (
    _ImplementContext,
    _ScopeGuardrailResult,
    _SinglePassResult,
    _is_binary_artifact,
)

if TYPE_CHECKING:
    from .phase_coordinator import ImplementStage

log = logging.getLogger("robotsix_mill.stages.implement")


class ValidationMixin:
    """Scope-guard / test-eval staticmethods mixed into :class:`ImplementStage`."""

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
        from robotsix_mill.stages import implement as _impl_pkg

        passed, diag = _impl_pkg.run_test_agent(
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
                smoke_paths = _impl_pkg.load_repo_smoke_paths(repo_dir)
                if smoke_paths_match(changed, smoke_paths):
                    smoke_passed, smoke_diag = _impl_pkg.run_smoke_agent(
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
