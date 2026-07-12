"""TicketService — the management-plane API surface over the DB.

All state mutation goes through here so the API, the worker, and tests
share one set of invariants (transition validation, history events,
workspace pointer upkeep). DB access is synchronous; the worker calls it
from its coroutine (never from the stage threadpool).

The implementation is split across responsibility mixins for navigability
(this package was previously a single ``service.py`` module): read/query
access (:mod:`._queries`), ticket creation (:mod:`._create_mixin`),
state transitions (:mod:`._transition_mixin`), cross-board migration
(:mod:`._migrate_mixin`), delete/redraft (:mod:`._delete_mixin`),
DB maintenance (:mod:`._maintenance_mixin`), single-column metadata
setters (:mod:`._metadata`), and comment/thread handling
(:mod:`._comments`). The module-level helpers and
:class:`TransitionError` live in :mod:`._helpers`. The concrete
:class:`TicketService` is assembled here by multiple inheritance; the
public import path ``robotsix_mill.core.service`` is unchanged.
"""

from __future__ import annotations

from collections.abc import Callable

from ...config import Settings
from ..models import Ticket
from ..states import State
from ..workspace import Workspace
from ._comments import _CommentMixin
from ._create_mixin import _CreateMixin
from ._delete_mixin import _DeleteMixin
from ._helpers import AmbiguousTicketId, TransitionError
from ._helpers import _event_hash as _event_hash
from ._helpers import _make_event as _make_event
from ._helpers import _parse_depends_on_str as _parse_depends_on_str
from ._helpers import _parse_labels as _parse_labels
from ._helpers import _prev_hash_for as _prev_hash_for
from ._helpers import _slug as _slug
from ._maintenance_mixin import _MaintenanceMixin
from ._metadata import _MetadataMixin
from ._migrate_mixin import _MigrateMixin
from ._queries import _QueryMixin
from ._transition_mixin import _TransitionMixin

__all__ = ["TicketService", "TransitionError", "AmbiguousTicketId"]


class TicketService(
    _CreateMixin,
    _TransitionMixin,
    _MigrateMixin,
    _DeleteMixin,
    _MaintenanceMixin,
    _QueryMixin,
    _MetadataMixin,
    _CommentMixin,
):
    """Manage the ticket lifecycle over per-repo SQLite databases.

    Central service for creating tickets, moving them through the state
    machine (raising :class:`TransitionError` on illegal transitions),
    persisting them to per-repo SQLite DBs, and keeping each ticket's
    :class:`Workspace` in sync. It is constructed from :class:`Settings`
    (which supplies the database path and the workspace root) and a
    *board_id* identifying the repository this instance is bound to;
    workspaces live under ``<data_dir>/<board_id>/workspaces/<ticket_id>/``,
    routed via :meth:`workspace`.

    Key method groups:

    * **Reads** — :meth:`get` (ID lookup with cross-repo fanout when
      ``board_id`` is empty), :meth:`list`, :meth:`history`,
      :meth:`list_children`, :meth:`list_comments`.
    * **Lifecycle / writes** — :meth:`create`, :meth:`transition`,
      :meth:`delete`, :meth:`add_comment`, :meth:`add_history_note`,
      :meth:`redraft`, :meth:`request_changes`, :meth:`mark_done`,
      :meth:`close_thread`, :meth:`reopen_thread`.
    * **Relationships / metadata** — :meth:`set_parent`,
      :meth:`set_unblocks`, :meth:`set_depends_on`,
      :meth:`promote_to_epic`, :meth:`set_priority`, :meth:`set_title`.
    """

    _ARCHIVABLE_STATES: set[State] = {State.CLOSED, State.ANSWERED, State.EPIC_CLOSED}

    def __init__(self, settings: Settings, board_id: str = "") -> None:
        """Create a service backed by the given :class:`Settings`.

        The settings provide the database path and workspace root directory.
        *board_id* identifies the repository this service stamps on tickets.
        """
        self.settings = settings
        self.board_id = board_id
        self._on_transition: "Callable[[Ticket], None] | None" = None

    def workspace(self, ticket: Ticket) -> Workspace:
        """Return the :class:`Workspace` for *ticket*.

        Routed via :meth:`Settings.workspaces_dir_for` using the
        ticket's ``board_id`` (falling back to this service's
        ``board_id``), so workspaces live under the per-repo subtree
        ``<data_dir>/<board_id>/workspaces/<ticket_id>/``.
        """
        board = ticket.board_id or self.board_id
        return Workspace(self.settings.workspaces_dir_for(board), ticket.id)
