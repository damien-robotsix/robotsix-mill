"""Review stage: IN_REVIEW -> DELIVERABLE (pass) or READY (changes).

STUB — to be implemented. Will run a quality/audit agent over the diff
and either pass it forward or bounce it back with required changes.
"""

from __future__ import annotations

from ..core.models import Ticket
from ..core.states import State
from .base import Outcome, Stage, StageContext


class ReviewStage(Stage):
    name = "review"
    input_state = State.IN_REVIEW

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        raise NotImplementedError("review stage not implemented yet")
