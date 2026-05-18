"""Refine stage: raw DRAFT -> actionable READY ticket.

Rewrites the file-canonical ``description.md`` into a precise spec the
implement agent can act on unattended; the original draft is kept as an
artifact for traceability. Empty draft or missing OpenRouter key ->
BLOCKED with a clear note (not a crash).

When ``require_approval`` is true (the default), the refined ticket
enters ``awaiting_approval`` instead of ``ready`` — a human must approve
before the implement stage picks it up.
"""

from __future__ import annotations

from ..agents import refining
from ..core.models import Ticket
from ..core.states import State
from .base import Outcome, Stage, StageContext


class RefineStage(Stage):
    name = "refine"
    input_state = State.DRAFT

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        ws = ctx.service.workspace(ticket)
        draft = ws.read_description().strip()
        if not draft:
            return Outcome(State.BLOCKED, "empty draft — nothing to refine")

        try:
            spec = refining.run_refine_agent(
                settings=ctx.settings, title=ticket.title, draft=draft
            )
        except RuntimeError as e:  # e.g. OPENROUTER_API_KEY not set
            return Outcome(State.BLOCKED, str(e))

        if not spec.strip():
            return Outcome(State.BLOCKED, "refiner produced an empty spec")

        # preserve the raw draft, then make the refined spec canonical
        (ws.artifacts_dir / "draft-original.md").write_text(
            draft, encoding="utf-8"
        )
        new_hash = ws.write_description(spec)
        ctx.service.set_content_hash(ticket.id, new_hash)

        next_state = (
            State.AWAITING_APPROVAL if ctx.settings.require_approval
            else State.READY
        )
        return Outcome(next_state, "refined")
