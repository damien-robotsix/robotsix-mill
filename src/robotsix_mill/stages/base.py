"""Stage contract.

A Stage consumes tickets in exactly one ``input_state``, does its work
(reading/writing the ticket's filesystem workspace), and returns an
:class:`Outcome` naming the next state. The worker applies the
transition — a stage never writes the DB directly.

``Stage.run`` is synchronous (LLM/tool calls). The worker runs it in a
threadpool so the API event loop stays responsive.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..config import Settings
from ..core.models import Ticket
from ..core.service import TicketService
from ..core.states import State


@dataclass
class StageContext:
    settings: Settings
    service: TicketService


@dataclass
class Outcome:
    """Result of processing one ticket."""

    next_state: State
    note: str | None = None


class Stage(ABC):
    #: unique stage name
    name: str
    #: the state this stage consumes
    input_state: State

    @abstractmethod
    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Process one ticket. Raise to fail the ticket (worker -> FAILED);
        raise NotImplementedError for a stub (worker logs, stops the
        chain, leaves the ticket)."""
        raise NotImplementedError
