"""Answer stage: picks up an ASKED inquiry, grounds the answering agent
in the real repo (best-effort clone), runs the agent, writes the answer
into ``description.md``, preserves the original question as an artifact,
and transitions to ANSWERED (or BLOCKED on failure).

Inquiries are read-only — no branches, no PRs, no forge side effects.
"""

from __future__ import annotations

import logging
import subprocess

from ..agents import answering
from ..core.models import Ticket
from ..core.states import State
from ..vcs import git_ops
from .base import Outcome, Stage, StageContext

log = logging.getLogger("robotsix_mill.stages.answer")


class AnswerStage(Stage):
    name = "answer"
    input_state = State.ASKED

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        ws = ctx.service.workspace(ticket)
        question = ws.read_description().strip()
        title = ticket.title.strip()
        if not title and not question:
            return Outcome(State.BLOCKED, "empty title and question — nothing to answer")

        s = ctx.settings

        # Clone the repo (best-effort) so the answering agent can
        # explore real code. Same pattern as RefineStage.
        repo_dir = None
        if s.forge_remote_url:
            cand = ws.dir / "repo"
            if (cand / ".git").exists():
                repo_dir = cand  # idempotent: reuse existing clone
            else:
                try:
                    git_ops.clone(
                        s.forge_remote_url, cand,
                        s.forge_target_branch, s.forge_token,
                    )
                    repo_dir = cand
                except subprocess.CalledProcessError as e:
                    log.warning(
                        "%s: answer clone failed, no repo grounding: %s",
                        ticket.id, (e.stderr or "")[:200],
                    )

        # --- preserve the original question ---
        (ws.artifacts_dir / "question-original.md").write_text(
            question if question else "(title-only inquiry, no body provided)",
            encoding="utf-8",
        )

        # --- run the answering agent ---
        try:
            answer = answering.run_answer_agent(
                settings=s,
                title=ticket.title,
                question=question,
                repo_dir=repo_dir,
            )
        except RuntimeError as e:  # e.g. OPENROUTER_API_KEY not set
            return Outcome(State.BLOCKED, str(e))

        if not answer or not answer.strip():
            return Outcome(State.BLOCKED, "answering agent produced an empty answer")

        # Write the answer into description.md (replaces the raw question)
        new_hash = ws.write_description(answer)
        ctx.service.set_content_hash(ticket.id, new_hash)

        return Outcome(State.ANSWERED, "answered")
