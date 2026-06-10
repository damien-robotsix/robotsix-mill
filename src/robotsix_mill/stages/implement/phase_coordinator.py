"""Multi-phase orchestration for the implement stage.

Assembles :class:`FileOperationsMixin`, :class:`ImplementationLogicMixin`,
and :class:`ValidationMixin` into the public :class:`ImplementStage`
``Stage`` subclass and holds the top-level orchestration: :meth:`run`
(dependency gate -> clone/branch/resume -> prerequisite + baseline gates
-> fix loop), :meth:`_implement_loop` (the bounded fix loop + circuit
breaker), and :meth:`_finalize` (artifact persistence + per-repo WIP
commit).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ...agents.testing import ENV_ERROR_PREFIX
from ...core.models import SourceKind, Ticket
from ...core.states import State
from ...forge.auth import _resolve_remote_url
from ...vcs import git_ops
from ..base import Outcome, Stage, StageContext
from ..pause import build_resume_message_history, load_conversation_state
from .file_operations import FileOperationsMixin
from .implementation_logic import ImplementationLogicMixin
from .validation import ValidationMixin

log = logging.getLogger("robotsix_mill.stages.implement")


class ImplementStage(
    ImplementationLogicMixin, ValidationMixin, FileOperationsMixin, Stage
):
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
            from ...meta.workspace import build_triaged_meta_workspace

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
        from ...hooks import run_prepare_hook

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
                    from ..pause import _collect_ask_user_replies

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
