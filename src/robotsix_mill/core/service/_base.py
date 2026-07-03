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

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from ...config import Settings
from ..models import Comment, Ticket, TicketEvent
from ..states import State
from ..workspace import Workspace

log = logging.getLogger("robotsix_mill.service")


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

    # --- board discovery ---

    def _collect_candidate_boards(
        self,
        caller_name: str,
        *,
        prepend_self: bool = False,
    ) -> list[str]:
        """Collect every known board id from the repos registry and a
        disk scan of ``data_dir``, deduplicated in registry-first order.

        *caller_name* is used in log messages so each call-site produces a
        distinct warning when the registry is unreachable.
        When *prepend_self* is ``True`` and ``self.board_id`` is truthy,
        the service's own ``board_id`` is prepended to the candidate list
        before the registry scan.
        """
        from ...config import get_repos_config

        candidates: list[str] = []
        if prepend_self and self.board_id:
            candidates.append(self.board_id)
        try:
            for rc in get_repos_config().repos.values():
                if rc.board_id and rc.board_id not in candidates:
                    candidates.append(rc.board_id)
        except Exception as exc:
            log.warning(
                "Failed to load repos config for %s: %s(%r)",
                caller_name,
                type(exc).__name__,
                exc,
            )
        # Disk-scan fallback for boards not in the registry.
        try:
            for sub in self.settings.data_dir.iterdir():
                if sub.is_dir() and (sub / "mill.db").exists():
                    if sub.name not in candidates:
                        candidates.append(sub.name)
        except OSError:
            pass
        return candidates
