"""Retrospect stage: DONE -> REVIEWED.

Post-delivery audit. Analyses the finished ticket's workflow (state
history + notes) and its Langfuse session (cost/latency/retries/errors,
workflow-only if Langfuse is unconfigured), records findings, and —
when MILL_RETROSPECT_SPAWN_DRAFTS is on and the agent proposes one —
files an improvement DRAFT linked back via parent_id. Then -> REVIEWED.

Agent/analysis failure is BLOCKED-resumable, never terminal.
"""

from __future__ import annotations

import logging

from .. import langfuse_client
from ..agents import retrospecting
from ..core.models import Ticket
from ..core.states import State
from .base import Outcome, Stage, StageContext

log = logging.getLogger("robotsix_mill.stages.retrospect")


class RetrospectStage(Stage):
    name = "retrospect"
    input_state = State.DONE

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        s = ctx.settings
        ws = ctx.service.workspace(ticket)

        history = ctx.service.history(ticket.id)
        history_text = "\n".join(
            f"{e.at:%Y-%m-%d %H:%M} {e.state} {e.note or ''}".rstrip()
            for e in history
        )
        ticket_summary = (
            f"id: {ticket.id}\ntitle: {ticket.title}\n"
            f"branch: {ticket.branch}\n\n{ws.read_description()[:6000]}"
        )
        lf = langfuse_client.fetch_session_summary(s, ticket.id)

        try:
            res = retrospecting.run_retrospect_agent(
                settings=s,
                ticket_summary=ticket_summary,
                history_text=history_text,
                langfuse_summary=lf,
            )
        except Exception as e:  # noqa: BLE001 — resumable, never lose the ticket
            log.exception("%s: retrospect agent failed", ticket.id)
            return Outcome(State.BLOCKED, f"retrospect failed — resumable: {e}")

        spawned = None
        if (
            s.retrospect_spawn_drafts
            and res.propose_draft
            and res.draft_title
            and res.draft_body
        ):
            draft = ctx.service.create(res.draft_title, res.draft_body)
            ctx.service.set_parent(draft.id, ticket.id)
            spawned = draft.id
            log.info("%s: retrospect spawned draft %s", ticket.id, spawned)

        (ws.artifacts_dir / "retrospect.md").write_text(
            f"# Retrospect\nlangfuse: "
            f"{'yes' if lf else 'workflow-only'}\n"
            f"spawned draft: {spawned or '—'}\n\n{res.findings}\n",
            encoding="utf-8",
        )
        note = "reviewed"
        if spawned:
            note = f"reviewed — improvement draft {spawned}"
        elif res.propose_draft and not s.retrospect_spawn_drafts:
            note = "reviewed — draft proposed (spawning disabled)"
        return Outcome(State.REVIEWED, note)
