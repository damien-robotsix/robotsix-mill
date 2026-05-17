"""Implement stage: READY -> IN_REVIEW (or BLOCKED).

Per ticket: fresh-clone the target repo into the ticket workspace,
branch, let the implement agent do the work via sandboxed tools, then
run the test command in a bounded fix loop. Pass -> IN_REVIEW. A
configuration/clone problem, no changes, or tests still failing after
``max_fix_attempts`` -> BLOCKED with a WIP commit left for inspection.
Pushing the branch + opening the MR happens later, in the deliver stage.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

from .. import sandbox
from ..agents import coding
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
        if not s.forge_remote_url:
            return Outcome(State.BLOCKED, "FORGE_REMOTE_URL not configured")

        ws = ctx.service.workspace(ticket)
        repo_dir = ws.dir / "repo"
        if repo_dir.exists():
            shutil.rmtree(repo_dir)  # fresh clone per ticket / per retry

        try:
            git_ops.clone(
                s.forge_remote_url,
                repo_dir,
                s.forge_target_branch,
                s.forge_token,
            )
        except subprocess.CalledProcessError as e:
            return Outcome(State.BLOCKED, f"clone failed: {e.stderr[:300]}")

        branch = f"{s.branch_prefix}{ticket.id}"
        git_ops.create_branch(repo_dir, branch)
        ctx.service.set_branch(ticket.id, branch)

        spec = ws.read_description()
        history: list | None = None
        feedback: str | None = None
        summary = ""

        for attempt in range(1, s.max_fix_attempts + 1):
            summary, history = coding.run_implement_agent(
                settings=s,
                repo_dir=repo_dir,
                spec=spec,
                feedback=feedback,
                history=history,
            )
            try:
                rc, output = self._run_tests(repo_dir, s)
            except sandbox.SandboxError as e:
                return Outcome(State.BLOCKED, f"sandbox unavailable: {e}")
            if rc == 0:
                if not git_ops.has_changes(repo_dir):
                    return Outcome(State.BLOCKED, "agent produced no changes")
                self._finalize(ctx, ticket, repo_dir, branch, summary, ok=True)
                return Outcome(
                    State.IN_REVIEW, (summary[:200] or "implemented")
                )
            log.info(
                "%s: tests failed attempt %d/%d",
                ticket.id, attempt, s.max_fix_attempts,
            )
            feedback = output

        # exhausted: keep the WIP branch so a human can pick up from here
        self._finalize(ctx, ticket, repo_dir, branch, summary, ok=False)
        return Outcome(
            State.BLOCKED,
            f"tests still failing after {s.max_fix_attempts} attempts",
        )

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
            f"# Implement ({'passed' if ok else 'BLOCKED — tests failing'})\n"
            f"branch: {branch}\n\n{summary}\n",
            encoding="utf-8",
        )
        if git_ops.has_changes(repo_dir):
            git_ops.commit_all(
                repo_dir,
                f"mill: {ticket.title} ({ticket.id})"
                + ("" if ok else " [WIP — tests failing]"),
            )
