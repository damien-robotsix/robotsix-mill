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

from ..config import RepoConfig, ReposRegistry, Settings
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


def stage_context_for(
    settings: Settings,
    board_id: str,
    repos: ReposRegistry,
) -> StageContext:
    """Build a :class:`StageContext` for the repo matching *board_id*.

    The ``repo_config`` is resolved by matching the ticket's
    ``board_id`` against the ``ReposRegistry``.  When no match is
    found, ``repo_config`` is ``None`` (backward compat for legacy
    tickets).  A fresh :class:`TicketService` is created with the
    correct ``board_id`` so new child tickets are stamped correctly.
    """
    from ..core.service import TicketService

    repo_config: RepoConfig | None = None
    for rc in repos.repos.values():
        if rc.board_id == board_id:
            repo_config = rc
            break
    service = TicketService(settings, board_id=board_id)
    return StageContext(settings=settings, service=service, repo_config=repo_config)


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

    @abstractmethod
    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Process one ticket. Raise to fail the ticket (worker -> FAILED);
        raise NotImplementedError for a stub (worker logs, stops the
        chain, leaves the ticket)."""
        raise NotImplementedError
