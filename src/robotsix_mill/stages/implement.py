"""Implement stage: READY -> IN_REVIEW (or BLOCKED, resumable).

First run: clone the target repo into the ticket workspace, branch, let
the implement agent work via sandboxed tools, run the test command in a
bounded fix loop. Pass -> IN_REVIEW.

Resume: if the ticket workspace already has the clone + its branch (a
prior BLOCKED run), do NOT re-clone — check the branch out and continue
from the committed WIP, replaying the persisted agent transcript so the
agent picks up where it stopped instead of restarting.

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

_TRANSCRIPT = "implement_messages.json"


class ImplementStage(Stage):
    name = "implement"
    input_state = State.READY

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        s = ctx.settings
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
            log.info("%s: resuming from existing WIP branch", ticket.id)
            git_ops.checkout(repo_dir, branch)
        else:
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
    def _load_transcript(ws) -> list | None:
        p = ws.artifacts_dir / _TRANSCRIPT
        if not p.exists():
            return None
        try:
            return coding.load_history(p.read_bytes())
        except Exception:  # noqa: BLE001 — corrupt transcript: start fresh
            log.warning("could not load transcript; starting agent fresh")
            return None

    @staticmethod
    def _save_transcript(ws, messages) -> None:
        if not messages:
            return
        try:
            (ws.artifacts_dir / _TRANSCRIPT).write_bytes(
                coding.dump_history(messages)
            )
        except Exception:  # noqa: BLE001 — never fail the stage on this
            log.warning("could not persist agent transcript")

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
