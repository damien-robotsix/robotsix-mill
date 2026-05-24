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
import shutil
import subprocess
from pathlib import Path

from ..agents import coding
from ..agents.coding import AgentBudgetError, AgentRunError
from ..agents.coordinating import ValidationResult
from ..agents.testing import run_test_agent
from ..config import get_secrets
from ..core.models import Ticket
from ..core.states import State
from ..pass_runner import load_memory, persist_memory
from ..vcs import git_ops
from .base import Outcome, Stage, StageContext

log = logging.getLogger("robotsix_mill.stages.implement")


class ImplementStage(Stage):
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
                ticket.id, unmet,
            )
            return Outcome(State.READY)

        if not s.forge_remote_url:
            return Outcome(State.BLOCKED, "FORGE_REMOTE_URL not configured")

        # Phase 1: clone and branch (or resume)
        result = ImplementStage._clone_and_branch(ctx, ticket, s)
        if isinstance(result, Outcome):
            return result
        repo_dir, branch, resuming = result

        # Phase 2: deterministic, stage-owned implement loop.
        return ImplementStage._implement_loop(
            ctx, ticket, repo_dir, branch, resuming, s
        )

    # --- helpers ---
    @staticmethod
    def _implement_loop(ctx, ticket, repo_dir, branch, resuming, settings):
        """Run the bounded fix loop: edit pass → test gate → route.

        The implement agent does ONE edit pass per iteration; the test
        gate runs the suite once and produces a distilled diagnosis;
        :meth:`ValidationResult.decide` routes deterministically. On
        ``retry`` the diagnosis is fed back into the next pass; on
        ``escalate`` (suite still failing after ``max_fix_iterations``)
        the ticket is BLOCKED-resumable. No LLM owns the loop or the
        bound — both are enforced here.
        """
        ws = ctx.service.workspace(ticket)
        spec = ws.read_description()
        epic_ctx = ctx.service.get_epic_context(ticket)
        if epic_ctx:
            spec = epic_ctx + "\n\n" + spec

        # Wire _epic/ if this ticket belongs to an epic
        epic_workspace_path: Path | None = None
        if ticket.parent_id:
            parent = ctx.service.get(ticket.parent_id)
            if parent and parent.kind == "epic":
                from ..core.workspace import link_epic_workspace
                epic_workspace_path = ctx.service.epic_workspace_dir(
                    ticket.parent_id
                )
                if not link_epic_workspace(repo_dir, epic_workspace_path):
                    # Repo has its own real _epic/ — skip epic wiring
                    epic_workspace_path = None

        memory_text = load_memory(settings.implement_memory_file)
        max_iters = max(1, settings.max_fix_iterations)

        # Load pre-loaded file content from the refine stage (if available).
        reference_files = None
        ref_files_path = ws.artifacts_dir / "reference_files.json"
        if ref_files_path.exists():
            reference_files = json.loads(ref_files_path.read_text(encoding="utf-8"))

        feedback: str | None = None
        summary = ""

        # If we're re-entering after a code review REQUEST_CHANGES, feed the
        # review comments as feedback so the coordinator can address them.
        review_feedback: str | None = None
        if ticket.blocked_from is None:  # not a BLOCKED resume
            comments = ctx.service.list_comments(ticket.id)
            if comments:
                review_feedback = "\n".join(
                    f"[REVIEW {c.created_at.isoformat()}] {c.body}"
                    for c in comments
                )
                feedback = review_feedback

        for attempt in range(1, max_iters + 1):
            try:
                summary, _, updated_memory = coding.run_implement_agent(
                    settings=settings, repo_dir=repo_dir, spec=spec,
                    feedback=feedback, memory=memory_text,
                    reference_files=reference_files,
                    epic_workspace_path=epic_workspace_path,
                )
            except AgentBudgetError as e:
                ImplementStage._finalize(
                    ctx, ticket, repo_dir, branch, f"budget cap hit: {e}",
                    ok=False,
                )
                return Outcome(
                    State.BLOCKED,
                    f"agent budget cap — resumable (move to READY): {e}",
                )
            except AgentRunError as e:
                ImplementStage._finalize(
                    ctx, ticket, repo_dir, branch, f"agent error: {e}",
                    ok=False,
                )
                return Outcome(
                    State.BLOCKED, f"agent error — resumable: {e}"
                )

            # Persist the agent's updated memory as soon as it's produced
            # so a later-iteration failure can't lose the learning.
            if updated_memory:
                persist_memory(settings.implement_memory_file, updated_memory)

            # Stage-owned test gate: one sandbox run; on failure a cheap
            # model distills an actionable diagnosis. `passed` is the
            # deterministic process exit code — the authoritative word.
            passed, diag = run_test_agent(
                settings=settings, repo_dir=repo_dir,
                epic_workspace_path=epic_workspace_path,
            )
            if not passed and diag.startswith("sandbox unavailable"):
                # Infra failure — not the code's fault; don't burn
                # iterations retrying against a broken sandbox.
                ImplementStage._finalize(
                    ctx, ticket, repo_dir, branch, summary, ok=False
                )
                return Outcome(State.BLOCKED, diag)

            decision = ValidationResult.decide(
                passed=passed, iterations=attempt, max_iters=max_iters,
                feedback=diag,
            )

            if decision.next_action == "proceed":
                if not git_ops.has_changes(repo_dir) and not resuming:
                    return Outcome(State.BLOCKED, "no changes produced")
                ImplementStage._finalize(
                    ctx, ticket, repo_dir, branch, summary, ok=True
                )
                next_state = (
                    State.CODE_REVIEW
                    if settings.review_enabled
                    else State.DOCUMENTING
                )
                return Outcome(
                    next_state, summary[:200] or "implemented"
                )

            if decision.next_action == "escalate":
                ImplementStage._finalize(
                    ctx, ticket, repo_dir, branch, summary, ok=False
                )
                return Outcome(
                    State.BLOCKED,
                    f"tests still failing after {max_iters} fix "
                    "attempt(s) — resumable (move to READY)",
                )

            # retry → feed the diagnosis into the next edit pass.
            feedback = diag

        # The escalate branch fires on the final attempt, so the loop
        # always returns above. This is a defensive fallback.
        ImplementStage._finalize(
            ctx, ticket, repo_dir, branch, summary, ok=False
        )
        return Outcome(
            State.BLOCKED, "implement loop exhausted — resumable"
        )

    @staticmethod
    def _finalize(ctx, ticket, repo_dir, branch, summary, *, ok: bool) -> None:
        ws = ctx.service.workspace(ticket)
        (ws.artifacts_dir / "implement.md").write_text(
            f"# Implement ({'passed' if ok else 'BLOCKED — resumable'})\n"
            f"branch: {branch}\n\n{summary}\n",
            encoding="utf-8",
        )
        if git_ops.has_changes(repo_dir):
            git_ops.commit_all(
                repo_dir,
                f"mill: {ticket.title} ({ticket.id})"
                + ("" if ok else " [WIP]"),
            )

    @staticmethod
    def _clone_and_branch(ctx, ticket, settings):
        ws = ctx.service.workspace(ticket)
        repo_dir = ws.dir / "repo"
        branch = f"{settings.branch_prefix}{ticket.id}"

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
                git_ops.clone(
                    settings.forge_remote_url,
                    repo_dir,
                    settings.forge_target_branch,
                    get_secrets().forge_token,
                )
            except subprocess.CalledProcessError as e:
                return Outcome(
                    State.BLOCKED, f"clone failed: {e.stderr[:300]}"
                )
            git_ops.create_branch(repo_dir, branch)

        # Refresh against current origin/<target> so the agent never
        # edits stale source — a branch based on even slightly outdated
        # origin/<target> can silently revert newer commits.
        if not git_ops.try_rebase_onto(repo_dir, settings.forge_target_branch):
            return Outcome(
                State.BLOCKED,
                f"rebase onto origin/{settings.forge_target_branch} "
                "failed — resolve conflicts manually",
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
                git_ops.clone(
                    settings.forge_remote_url, repo_dir,
                    settings.forge_target_branch, get_secrets().forge_token,
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
