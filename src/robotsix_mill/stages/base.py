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

from ..config import RepoConfig, Settings
from ..core.models import Ticket
from ..core.service import TicketService
from ..core.states import State


@dataclass
class StageContext:
    """Dependency-injection context passed to every :meth:`Stage.run`.

    Attributes:
        settings: Mill runtime configuration (models, paths, limits).
        service: :class:`TicketService` for reading ticket state and
            workspace.
        repo_config: Per-repository configuration resolved from the
            ``--repo-id`` CLI argument (board identity, Langfuse
            credentials for the repository).
    """

    settings: Settings
    service: TicketService
    repo_config: RepoConfig | None = None

    def memory_board_id(self, ticket: Ticket) -> str:
        """Resolve the board_id for ``settings.memory_file_for(...)``.

        ``repo_config`` is ``None`` for the synthetic meta board (not a
        registered repo), so the old ``repo_config.board_id if repo_config
        else ""`` crashed meta tickets with "memory_file_for: board_id is
        required" once the board-less memory fallback was removed. Fall back
        to the bound service board, then the ticket's own board (both are
        ``"meta"`` for a meta ticket).
        """
        return (
            (self.repo_config.board_id if self.repo_config else "")
            or self.service.board_id
            or ticket.board_id
        )


@dataclass
class Outcome:
    """Result of processing one ticket."""

    next_state: State
    note: str | None = None


class Stage(ABC):
    """One step in the ticket pipeline.

    Implementors must set ``name`` and ``input_state``, and implement
    :meth:`run`. Set ``traced = False`` for poll-driven, no-LLM stages
    (e.g. merge, deliver) to avoid spamming empty Langfuse traces.
    """

    #: unique stage name
    name: str
    #: the state this stage consumes
    input_state: State
    #: emit a Langfuse trace for this stage? False for poll-driven,
    #: no-LLM stages (merge, deliver) so polling doesn't spam empty
    #: "ticket" traces into the session.
    traced: bool = True

    def preflight(self, ticket: Ticket, ctx: StageContext) -> Outcome | None:
        """Lightweight pre-trace gate — run BEFORE any Langfuse trace is opened.

        Return an :class:`Outcome` to short-circuit the stage dispatch
        without opening a trace or incrementing the dispatch counter.
        Return ``None`` to proceed normally (trace opens, :meth:`run` called).

        The default implementation returns ``None`` (always proceed).
        Stages that have cheap early-exit conditions should override.
        """
        return None

    @abstractmethod
    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Process one ticket. Raise to fail the ticket (worker -> FAILED);
        raise NotImplementedError for a stub (worker logs, stops the
        chain, leaves the ticket)."""
        raise NotImplementedError
