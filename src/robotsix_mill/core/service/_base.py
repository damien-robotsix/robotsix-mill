"""Typed base shared by the ``TicketService`` mixins.

The single ``TicketService`` class is split across responsibility mixins
(``_queries``, ``_lifecycle``, ``_comments``, ``_actions``). Each mixin
calls methods and reads attributes that are physically defined in a
*sibling* mixin or set by ``TicketService.__init__``. ``_ServiceBase``
declares that shared surface so each mixin type-checks in isolation under
``mypy --strict``; the real implementations are supplied by the assembled
class via the MRO at runtime (the method declarations below live under
``TYPE_CHECKING`` and therefore do not exist as runtime attributes).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from ...config import Settings
from ..models import Comment, Ticket, TicketEvent
from ..states import State
from ..workspace import Workspace


class _ServiceBase:
    """Shared state and cross-mixin method surface for the service mixins."""

    settings: Settings
    board_id: str
    _on_transition: Callable[[Ticket], None] | None
    _ARCHIVABLE_STATES: set[State]

    if TYPE_CHECKING:

        def workspace(self, ticket: Ticket) -> Workspace:
            pass

        def get(self, ticket_id: str) -> Ticket | None:
            pass

        def _board_for(self, ticket_id: str) -> str:
            pass

        def _all_descendants(self, ticket_id: str) -> list[Ticket]:
            pass

        def transition(
            self, ticket_id: str, dst: State, note: str | None = ...
        ) -> Ticket:
            pass

        def add_comment(
            self,
            ticket_id: str,
            body: str,
            author: str = ...,
            parent_id: int | None = ...,
        ) -> Comment:
            pass

        def add_history_note(self, ticket_id: str, note: str) -> TicketEvent:
            pass

        def set_labels(self, ticket_id: str, labels: list[str]) -> Ticket:
            pass

        # Cross-mixin calls introduced by the lifecycle split.
        def _has_open_ask_user_threads(
            self, ticket_id: str, session: object
        ) -> list[Comment]:
            pass

        def _maybe_purge_archived(self) -> None:
            pass

        def _has_active_child(self, ticket_id: str) -> bool:
            pass

        def delete(self, ticket_id: str) -> bool:
            pass
