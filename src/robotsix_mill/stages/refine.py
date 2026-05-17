"""Refine stage: raw DRAFT -> actionable READY ticket.

STUB — to be implemented. Will use a pydantic-ai agent to rewrite the
draft description into a well-formed, actionable spec.
"""

from __future__ import annotations

from ..core.models import Ticket
from ..core.states import State
from .base import Outcome, Stage, StageContext


class RefineStage(Stage):
    name = "refine"
    input_state = State.DRAFT

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        raise NotImplementedError("refine stage not implemented yet")
