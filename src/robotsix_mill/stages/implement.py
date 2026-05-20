"""Implement stage: READY -> IN_REVIEW (or BLOCKED, resumable).

First run: clone the target repo into the ticket workspace, branch, let
the implement agent work via sandboxed tools, run the test command in a
bounded fix loop. Pass -> IN_REVIEW.

Resume: if the ticket workspace already has the clone + its branch (a
prior BLOCKED run), do NOT re-clone — check the branch out and continue
from the committed WIP.

Everything that isn't success is BLOCKED-resumable with WIP committed:
no remote, clone failure, no changes, sandbox down, agent error/budget
cap, or tests still failing after ``max_fix_attempts``. Pushing the
branch + opening the MR happens later, in the deliver stage.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

from .. import sandbox
from ..agents import coding
from ..agents.coding import AgentBudgetError, AgentRunError
from ..core.models import Ticket
from ..core.states import State
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

        ws = ctx.service.workspace(ticket)
        repo_dir = ws.dir / "repo"
        branch = f"{s.branch_prefix}{ticket.id}"

        # Resume iff a prior run left this ticket's clone + branch behind.
        resuming = (repo_dir / ".git").exists() and git_ops.branch_exists(
            repo_dir, branch
        )
        if resuming:
            git_ops.checkout(repo_dir, branch)
            # Refresh the WIP branch onto current target before running:
            # a branch pinned to an OLD base runs the in-sandbox test
            # gate against stale code (e.g. a pre-fix conftest) and
            # re-BLOCKS forever even after main is fixed. If it can't
            # rebase cleanly, discard the stale WIP and re-clone fresh
            # (a gate-blocked WIP was against a broken gate anyway).
            if git_ops.try_rebase_onto(repo_dir, s.forge_target_branch):
                log.info(
                    "%s: resuming WIP branch (rebased onto %s)",
                    ticket.id, s.forge_target_branch,
                )
            else:
                log.warning(
                    "%s: WIP rebase onto %s failed — re-cloning fresh",
                    ticket.id, s.forge_target_branch,
                )
                resuming = False
        if not resuming:
            if repo_dir.exists():
                shutil.rmtree(repo_dir)
            try:
                git_ops.clone(
                    s.forge_remote_url,
                    repo_dir,
                    s.forge_target_branch,
                    s.forge_token,
                )
            except subprocess.CalledProcessError as e:
                return Outcome(
                    State.BLOCKED, f"clone failed: {e.stderr[:300]}"
                )
            git_ops.create_branch(repo_dir, branch)

        # Hard invariant: NEVER run the agent / sandbox without a
        # materialized clone. A repo that was pruned after a prior
        # delivery, rmtree'd by the resume path, or never created
        # would otherwise blow up deep in the test sandbox with a
        # confusing "repo not cloned" — *after* burning the expensive
        # coordinator. Re-clone here; if that fails it's a clean,
        # resumable BLOCK (next run re-clones) instead.
        if not (repo_dir / ".git").exists():
            log.warning(
                "%s: clone missing before agent run — re-cloning",
                ticket.id,
            )
            if repo_dir.exists():
                shutil.rmtree(repo_dir, ignore_errors=True)
            try:
                git_ops.clone(
                    s.forge_remote_url, repo_dir,
                    s.forge_target_branch, s.forge_token,
                )
                git_ops.create_branch(repo_dir, branch)
            except subprocess.CalledProcessError as e:
                return Outcome(
                    State.BLOCKED,
                    "repo clone missing and re-clone failed — "
                    f"resumable: {(e.stderr or '')[:200]}",
                )
        ctx.service.set_branch(ticket.id, branch)

        spec = ws.read_description()

        # The coordinator owns the explore→plan→implement→test loop
        # (it re-explores fresh on a resume — no transcript needed).
        try:
            summary, _ = coding.run_implement_agent(
                settings=s, repo_dir=repo_dir, spec=spec,
            )
        except AgentBudgetError as e:
            self._finalize(
                ctx, ticket, repo_dir, branch, f"budget cap hit: {e}",
                ok=False,
            )
            return Outcome(
                State.BLOCKED,
                f"agent budget cap — resumable (move to READY): {e}",
            )
        except AgentRunError as e:
            self._finalize(
                ctx, ticket, repo_dir, branch, f"agent error: {e}",
                ok=False,
            )
            return Outcome(
                State.BLOCKED, f"agent error — resumable: {e}"
            )

        # Authoritative final gate: the coordinator already looped via
        # the test sub-agent, but the stage re-verifies once as the
        # trusted word before delivering.
        try:
            rc, _ = self._run_tests(repo_dir, s)
        except sandbox.SandboxError as e:
            self._finalize(ctx, ticket, repo_dir, branch, summary, ok=False)
            return Outcome(State.BLOCKED, f"sandbox unavailable: {e}")

        if rc != 0:
            self._finalize(ctx, ticket, repo_dir, branch, summary, ok=False)
            return Outcome(
                State.BLOCKED,
                "coordinator finished but the test gate still fails "
                "— resumable (move to READY)",
            )
        if not git_ops.has_changes(repo_dir) and not resuming:
            return Outcome(State.BLOCKED, "no changes produced")
        self._finalize(ctx, ticket, repo_dir, branch, summary, ok=True)
        return Outcome(State.DELIVERABLE, summary[:200] or "implemented")

    # --- helpers ---
    @staticmethod
    def _run_tests(repo_dir, settings) -> tuple[int, str]:
        cmd = settings.test_command.strip()
        if not cmd:
            return 0, ""  # test gate disabled
        # same sandbox as the agent's run_command: isolated, no network
        return sandbox.run(cmd, repo_dir=repo_dir, settings=settings)

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
