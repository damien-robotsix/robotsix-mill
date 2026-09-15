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
    _on_transition: Callable[[Ticket, str], None] | None
    _ARCHIVABLE_STATES: set[State]

    if TYPE_CHECKING:

        def workspace(self, ticket: Ticket) -> Workspace:
            """Return the :class:`Workspace` for *ticket* (impl:
            ``TicketService.workspace``).
            """

        def get(self, ticket_id: str) -> Ticket | None:
            """Look up a :class:`Ticket` by id, or ``None`` (impl:
            ``_QueryMixin.get``).
            """

        def _board_for(self, ticket_id: str) -> str:
            pass

        def _all_descendants(self, ticket_id: str) -> list[Ticket]:
            pass

        def transition(
            self,
            ticket_id: str,
            dst: State,
            note: str | None = ...,
            block_reason: str | None = ...,
        ) -> Ticket:
            """Move a ticket to *dst* state, recording history and (for
            BLOCKED) ``blocked_from`` / ``block_reason`` (impl:
            ``_TransitionMixin.transition``).
            """

        def add_comment(
            self,
            ticket_id: str,
            body: str,
            author: str = ...,
            parent_id: int | None = ...,
        ) -> Comment:
            """Add a reviewer/reply comment to a ticket (impl:
            ``_CommentMixin.add_comment``).
            """

        def add_history_note(self, ticket_id: str, note: str) -> TicketEvent:
            """Append a non-transition informational history entry (impl:
            ``_CreateMixin.add_history_note``).
            """

        def set_labels(self, ticket_id: str, labels: list[str]) -> Ticket:
            """Replace the free-form label list on *ticket_id* (impl:
            ``_MetadataMixin.set_labels``).
            """

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
            """Hard-delete a ticket, its history and workspace; ``False``
            if unknown (impl: ``_DeleteMixin.delete``).
            """

        def get_epic_context(self, ticket: Ticket) -> str:
            """Return the parent epic's description as an ``epic-context``
            block, or ``""`` (impl: ``_QueryMixin.get_epic_context``).
            """

        def _compute_spec_fingerprint(self, ticket: Ticket) -> str:
            pass

        def close_tracker(self, ticket_id: str, note: str = ...) -> Ticket:
            """Close a tracker ticket from any non-terminal state, skipping
            merge/branch checks (impl: ``_TransitionMixin.close_tracker``).
            """

        # Cross-mixin calls introduced by the dependency-edit endpoint.
        def _parse_depends_on(self, ticket: Ticket) -> list[str]:
            pass

        def unmet_dependencies(self, ticket: Ticket) -> list[str]:
            """Return *ticket*'s ``depends_on`` IDs not yet in a terminal
            state (impl: ``_QueryMixin.unmet_dependencies``).
            """

        def resume_blocked(self, ticket_id: str, note: str = ...) -> Ticket:
            """Resume a blocked ticket to its ``blocked_from`` state (impl:
            ``_TransitionMixin.resume_blocked``).
            """

    # --- board discovery ---

    def _collect_candidate_boards(
        self,
        caller_name: str,
    ) -> list[str]:
        """Collect every known board id from the repos registry and a
        disk scan of ``data_dir``, deduplicated in registry-first order.

        *caller_name* is used in log messages so each call-site produces a
        distinct warning when the registry is unreachable.
        The service's own ``board_id`` (when non-empty) is always included
        first, before the registry scan.
        """
        from ...config import get_repos_config

        candidates: list[str] = []
        if self.board_id:
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
        # When the default repo is not registered in repos.yaml but its
        # on-disk DB exists (mill's own board managing external repos),
        # include it so cross-board lookups from unbound services can
        # find tickets filed on the mill board.
        default_repo = self.settings.default_repo_id
        if default_repo and default_repo not in candidates:
            if (self.settings.data_dir / default_repo / "mill.db").exists():
                candidates.append(default_repo)
        return candidates
