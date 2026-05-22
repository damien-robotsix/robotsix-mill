"""Review stage: CODE_REVIEW -> DELIVERABLE | READY | BLOCKED.

Runs a blind dual-model review of the implementation diff. The review
agent sees ONLY the git diff and ticket spec — no implementation
context.  APPROVE → DELIVERABLE; REQUEST_CHANGES → READY (with review
comments stored); NEEDS_DISCUSSION → BLOCKED (with comments stored).
"""

from __future__ import annotations

import logging

from ..agents.reviewing import ReviewVerdict, run_review_agent
from ..core.models import Ticket
from ..core.states import State
from ..vcs import git_ops
from .base import Outcome, Stage, StageContext

log = logging.getLogger("robotsix_mill.stages.review")


class ReviewStage(Stage):
    name = "review"
    input_state = State.CODE_REVIEW
    traced = True

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        s = ctx.settings

        ws = ctx.service.workspace(ticket)
        repo_dir = ws.dir / "repo"

        # Guard: missing clone → BLOCKED (resumable: re-run implement)
        if not (repo_dir / ".git").exists():
            return Outcome(
                State.BLOCKED,
                "no repository clone to review (re-run implement)",
            )

        target_branch = s.forge_target_branch

        # Compute diff of all commits on the current branch vs origin/<target>.
        try:
            diff = git_ops.diff_base(repo_dir, target_branch)
        except Exception as e:
            return Outcome(
                State.BLOCKED,
                f"failed to compute diff: {e}",
            )

        # Empty diff → no-op implementation, approve so deliver can handle it.
        if not diff.strip():
            log.info("%s: empty diff — approving without review", ticket.id)
            return Outcome(State.DELIVERABLE, "empty diff (no-op implementation)")

        spec = ws.read_description()

        # Run the blind review agent.
        try:
            verdict: ReviewVerdict = run_review_agent(
                settings=s, diff=diff, spec=spec,
            )
        except Exception as e:
            log.exception("%s: review agent error", ticket.id)
            return Outcome(
                State.BLOCKED,
                f"review agent error — resumable: {e}",
            )

        # Persist review artifact for downstream consumers (e.g. auto-merge).
        ws.artifacts_dir.joinpath("review.md").write_text(
            f"verdict: {verdict.verdict}\n"
            f"auto_merge_eligible: {str(verdict.auto_merge_eligible).lower()}\n",
            encoding="utf-8",
        )

        # Route based on verdict.
        if verdict.verdict == "APPROVE":
            ctx.service.set_review_rounds(ticket.id, 0)
            return Outcome(State.DELIVERABLE, "review approved")
        elif verdict.verdict == "REQUEST_CHANGES":
            rounds = ticket.review_rounds + 1
            ctx.service.set_review_rounds(ticket.id, rounds)
            if rounds >= s.review_max_rounds:
                ctx.service.add_comment(
                    ticket.id,
                    f"Review round cap exhausted ({rounds}/{s.review_max_rounds} "
                    f"REQUEST_CHANGES rounds). Escalating to DELIVERABLE for "
                    f"human merge approval.\n\nLast review verdict:\n{verdict.comments}",
                )
                ctx.service.set_review_rounds(ticket.id, 0)
                return Outcome(
                    State.DELIVERABLE,
                    f"review rounds exhausted ({rounds}/{s.review_max_rounds})",
                )
            # under cap: normal REQUEST_CHANGES path
            ctx.service.add_comment(ticket.id, verdict.comments, author="review")
            return Outcome(
                State.READY,
                verdict.comments[:200],
            )
        else:  # NEEDS_DISCUSSION
            ctx.service.add_comment(ticket.id, verdict.comments, author="review")
            return Outcome(
                State.BLOCKED,
                verdict.comments[:200],
            )
