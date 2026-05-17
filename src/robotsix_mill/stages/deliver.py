"""Deliver stage: DELIVERABLE -> DONE.

STUB — to be implemented. Will push the ticket branch and open a
merge/pull request via the configured forge adapter.
"""

from __future__ import annotations

from ..core.models import Ticket
from ..core.states import State
from .base import Outcome, Stage, StageContext


class DeliverStage(Stage):
    name = "deliver"
    input_state = State.DELIVERABLE

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        raise NotImplementedError("deliver stage not implemented yet")
