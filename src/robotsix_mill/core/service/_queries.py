"""Read-only query surface of :class:`TicketService` (``_QueryMixin``)."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import TYPE_CHECKING

from sqlmodel import select

from .. import db
from ...config import Settings
from ..models import (
    Comment,
    SourceKind,
    Ticket,
    TicketEvent,
    TicketKind,
)
from ..states import ASK_USER_MARKER, State
from ._base import _ServiceBase
from ._helpers import AmbiguousTicketId, _get_ticket, _parse_depends_on_str

if TYPE_CHECKING:
    from ...config import RepoConfig

log = logging.getLogger("robotsix_mill.service")


class _QueryMixin(_ServiceBase):
    """Read-only access: lookups, listings, and history."""

    # --- reads ---
    def get(self, ticket_id: str) -> Ticket | None:
        """Look up a :class:`Ticket` by id, or return ``None``.

        With per-repo DBs, callers that don't carry a ``board_id``
        (most prominently the agent tools at
        ``agents/read_ticket.py``, ``close_thread.py``,
        ``reply_thread.py``) need a single ID-based lookup that
        works across every repo. When ``self.board_id`` is empty,
        fan out: try the default DB first (legacy / repo-less rows),
        then each registered repo's DB until we find the ticket.
        """
        if not self.board_id:
            ticket, _ = self._get_anywhere(ticket_id)
            return ticket
        with db.session(self.settings, self.board_id) as s:
            ticket = s.get(Ticket, ticket_id)
        if ticket is not None:
            self._resolve_board_id(ticket)
            return ticket
        # Bound-board miss — fall back to fanout. With per-repo DBs
        # the worker's & routes' default service is pinned to the
        # first repo, so any ticket in another repo's DB would 404
        # without this fallback.
        ticket, _ = self._get_anywhere(ticket_id)
        return ticket

    def resolve_by_suffix(self, suffix: str) -> str | None:
        """Return the full ticket ID whose id ends with *suffix*.

        Searches every configured board.  Returns ``None`` when no
        ticket matches, and raises :class:`AmbiguousTicketId` when
        more than one ticket matches.
        """
        candidates = self._collect_candidate_boards(caller_name="resolve_by_suffix")
        matches: list[str] = []
        for board_id in candidates:
            with db.session(self.settings, board_id) as s:
                stmt = select(Ticket).where(Ticket.id.endswith(suffix))
                for ticket in s.exec(stmt).all():
                    if ticket.id not in matches:
                        matches.append(ticket.id)
        if not matches:
            return None
        if len(matches) > 1:
            raise AmbiguousTicketId(
                f"Ambiguous suffix '{suffix}': matches {len(matches)} tickets"
            )
        return matches[0]

    def _get_anywhere(self, ticket_id: str) -> tuple[Ticket | None, list[str]]:
        """Search every per-repo DB for *ticket_id*. Ticket IDs are
        globally unique so the first hit is the answer.

        Discovers candidate boards two ways so we don't miss any:
        1. From the registered :class:`ReposRegistry` (production path
           — repos.yaml configured).
        2. By scanning ``data_dir`` for ``<board>/mill.db`` files
           (robust to test setups that don't register repos but write
           per-board DBs via the migration / direct-board service).
        """
        candidates = self._collect_candidate_boards(caller_name="_get_anywhere")
        for board_id in candidates:
            with db.session(self.settings, board_id) as s:
                ticket = s.get(Ticket, ticket_id)
                if ticket is not None:
                    self._resolve_board_id(ticket)
                    return ticket, candidates
        return None, candidates

    # Fields that are safe to use as ``sort_by`` values — caller-supplied
    # strings that map directly to Ticket column attributes.  Kept as a
    # frozenset to reject unsupported fields before they reach the query.
    _SORTABLE_FIELDS: frozenset[str] = frozenset(
        {"created_at", "updated_at", "title", "state", "priority", "kind"}
    )

    def list(
        self,
        state: State | None = None,
        exclude_states: Iterable[State] | None = None,
        *,
        offset: int = 0,
        limit: int | None = None,
        sort_by: str = "created_at",
        created_after: datetime | None = None,
    ) -> list[Ticket]:
        """List tickets, optionally filtered by *state* or excluding
        *exclude_states* (e.g. terminal CLOSED/DONE for a fast board).

        Pagination:
          *offset* — rows to skip (default 0).
          *limit*  — max rows to return (``None`` = unbounded).

        Sorting:
          *sort_by* — column name from ``_SORTABLE_FIELDS`` (default
          ``"created_at"``).  Raises ``ValueError`` for unsupported
          fields.

        Filtering:
          *created_after* — only return tickets whose ``created_at`` is
          strictly after this UTC datetime.

        Results are ordered by *sort_by* ascending.
        """
        if sort_by not in self._SORTABLE_FIELDS:
            raise ValueError(
                f"sort_by must be one of {sorted(self._SORTABLE_FIELDS)}, "
                f"got {sort_by!r}"
            )
        order_col: object = getattr(Ticket, sort_by)
        with db.session(self.settings, self.board_id) as s:
            stmt = select(Ticket).order_by(order_col)  # type: ignore[arg-type]
            if state is not None:
                stmt = stmt.where(Ticket.state == state)
            if exclude_states:
                stmt = stmt.where(Ticket.state.notin_(list(exclude_states)))
            if created_after is not None:
                stmt = stmt.where(Ticket.created_at > created_after)
            if offset:
                stmt = stmt.offset(offset)
            if limit is not None:
                stmt = stmt.limit(limit)
            tickets = list(s.exec(stmt).all())
        for t in tickets:
            self._resolve_board_id(t)
        return tickets

    def _board_for(self, ticket_id: str) -> str:
        """Resolve the actual board that holds *ticket_id*.

        Returns ``self.board_id`` when the bound DB has the row, else
        fans out via ``_get_anywhere`` and returns the discovered
        board.  Raises ``ValueError`` when the ticket cannot be found
        in any configured board and ``self.board_id`` is empty.
        """
        if self.board_id:
            with db.session(self.settings, self.board_id) as s:
                if s.get(Ticket, ticket_id) is not None:
                    return self.board_id
        t, candidates = self._get_anywhere(ticket_id)
        if t is not None:
            return t.board_id or self.board_id or ""
        if self.board_id:
            return self.board_id
        raise ValueError(
            f"Ticket {ticket_id} not found in any configured board "
            f"(searched: {candidates or '<none>'})"
        )

    def _resolve_board_id(self, ticket: Ticket) -> None:
        """Assign *ticket* a ``board_id`` when it is missing (legacy rows).

        Legacy tickets (created before multi-repo support) have an empty
        ``board_id``.  They are assigned ``settings.default_repo_id`` at
        read time when it is configured.  When ``default_repo_id`` is
        also empty the ticket is left as-is (the operator must configure
        the default before multi-repo routing can work for legacy rows).
        """
        if ticket.board_id:
            return
        default = self.settings.default_repo_id
        if default:
            ticket.board_id = default

    def history(self, ticket_id: str) -> list[TicketEvent]:
        """Return the :class:`TicketEvent` log for *ticket_id*, ordered by ``at``."""
        board = self._board_for(ticket_id)
        with db.session(self.settings, board) as s:
            stmt = (
                select(TicketEvent)
                .where(TicketEvent.ticket_id == ticket_id)
                .order_by(TicketEvent.at)
            )
            return list(s.exec(stmt).all())

    def recent_proposals_for(
        self,
        source: SourceKind,
        limit: int = 100,
    ) -> list[Ticket]:
        """Return up to *limit* tickets from *source*, most recent first."""
        with db.session(self.settings, self.board_id) as s:
            stmt = (
                select(Ticket)
                .where(Ticket.source == source)
                .order_by(Ticket.created_at.desc())
                .limit(limit)
            )
            return list(s.exec(stmt).all())

    def recent_tickets(
        self,
        limit: int = 100,
        *,
        sources: Sequence[SourceKind] | None = None,
        board_id: str | None = None,
    ) -> list[Ticket]:
        """Return up to *limit* tickets, most recent first.

        Source-agnostic counterpart to :meth:`recent_proposals_for`:
        ``sources=None`` returns recent tickets across ALL sources,
        while a sequence unions the listed source kinds. *board_id*
        overrides the service's own board when given.
        """
        board = board_id if board_id is not None else self.board_id
        with db.session(self.settings, board) as s:
            stmt = select(Ticket)
            if sources is not None:
                stmt = stmt.where(Ticket.source.in_(list(sources)))
            stmt = stmt.order_by(Ticket.created_at.desc()).limit(limit)
            return list(s.exec(stmt).all())

    def list_children(self, ticket_id: str) -> list[Ticket]:
        """Return all tickets whose ``parent_id`` equals *ticket_id*."""
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            stmt = select(Ticket).where(Ticket.parent_id == ticket_id)
            return list(s.exec(stmt).all())

    def list_children_across_boards(self, parent_id: str) -> list[Ticket]:
        """Return all tickets whose ``parent_id`` equals *parent_id*,
        searching every configured board.

        Mirrors the board-enumeration in :meth:`_get_anywhere`: collects
        candidate boards from the :class:`ReposRegistry` AND a disk scan
        of ``data_dir``, then queries each board for children.  Returns
        the union — a child on any board whose ``parent_id`` matches
        *parent_id* is included.

        Each returned ticket has its ``board_id`` resolved via
        :meth:`_resolve_board_id`.
        """
        candidates = self._collect_candidate_boards(
            caller_name="list_children_across_boards"
        )

        result: list[Ticket] = []
        seen: set[str] = set()
        for board_id in candidates:
            with db.session(self.settings, board_id) as s:
                for child in s.exec(
                    select(Ticket).where(Ticket.parent_id == parent_id)
                ).all():
                    if child.id not in seen:
                        seen.add(child.id)
                        self._resolve_board_id(child)
                        result.append(child)
        return result

    def cumulative_cost(
        self,
        ticket_id: str,
        settings: Settings,
        *,
        blocking: bool = True,
        repo_config: "RepoConfig | None" = None,
    ) -> float:
        """Return the cumulative cost of *ticket_id* and all descendants (recursive).

        Uses the same blocking/cache-only mode as the caller — blocking
        for per-ticket detail views, cache-only for the polled /tickets list.

        When *repo_config* is provided, its Langfuse credentials are used
        for the cost lookup (per-repo isolation).
        """
        from ...langfuse.client import session_cost, session_cost_cached

        def cost_fn(sid: str) -> float:
            if blocking:
                return session_cost(settings, sid, repo_config=repo_config)
            return session_cost_cached(sid, repo_config=repo_config)

        total = cost_fn(ticket_id)
        for descendant in self._all_descendants(ticket_id):
            total += cost_fn(descendant.id)
        return total

    def _all_descendants(self, ticket_id: str) -> list[Ticket]:
        """Return every descendant of *ticket_id* at any depth (BFS, cycle-safe)."""
        result: list[Ticket] = []
        visited: set[str] = {ticket_id}
        queue: list[str] = [ticket_id]
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            while queue:
                parent = queue.pop(0)
                children = list(
                    s.exec(select(Ticket).where(Ticket.parent_id == parent)).all()
                )
                for child in children:
                    if child.id not in visited:
                        visited.add(child.id)
                        result.append(child)
                        queue.append(child.id)
        return result

    def get_epic_context(self, ticket: Ticket) -> str:
        """Return the epic description wrapped in an ``epic-context``
        fenced block if *ticket* has a parent whose ``kind`` is
        ``"epic"``, or ``""`` otherwise."""
        if ticket.parent_id is None:
            return ""
        parent = self.get(ticket.parent_id)
        if parent is None or parent.kind != TicketKind.EPIC:
            return ""
        desc = self.workspace(parent).read_description()
        if not desc:
            return ""
        from ...agents.prompt_blocks import section

        return section("epic-context", desc)

    # --- dependency helpers ---

    @staticmethod
    def _parse_depends_on(ticket: Ticket) -> list[str]:
        """Parse the JSON list of dependency IDs from *ticket*."""
        return _parse_depends_on_str(ticket.depends_on)

    def unmet_dependencies(self, ticket: Ticket) -> list[str]:
        """Return the subset of *ticket*'s ``depends_on`` IDs that are
        NOT in a terminal state (CLOSED or DONE).

        * A missing/deleted dep ID is treated as satisfied (warning).
        * A dep that itself directly depends on *ticket* (cycle A↔B) is
          treated as satisfied (warning).
        """
        dep_ids = self._parse_depends_on(ticket)
        if not dep_ids:
            return []

        unmet: list[str] = []
        for dep_id in dep_ids:
            dep_ticket = self.get(dep_id)
            if dep_ticket is None:
                log.debug(
                    "ticket %s: dependency %s not found — treating as satisfied",
                    ticket.id,
                    dep_id,
                )
                continue

            # Direct cycle: A → B, B → A
            dep_deps = self._parse_depends_on(dep_ticket)
            if ticket.id in dep_deps:
                log.debug(
                    "ticket %s: direct cycle with dependency %s — treating as satisfied",
                    ticket.id,
                    dep_id,
                )
                continue

            if dep_ticket.state in (State.CLOSED, State.DONE):
                continue

            unmet.append(dep_id)

        return unmet

    # --- comments ---
    def list_comments(self, ticket_id: str) -> list[Comment]:
        """Return all comments for *ticket_id*, ordered oldest-first.
        Raises ``KeyError`` if the ticket does not exist."""
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            _get_ticket(s, ticket_id)
            stmt = (
                select(Comment)
                .where(Comment.ticket_id == ticket_id)
                .order_by(Comment.created_at)
            )
            return list(s.exec(stmt).all())

    def pending_question(self, ticket_id: str) -> str | None:
        """Return the verbatim clarifying-question text of the most recent
        OPEN top-level ``[ASK_USER]`` comment on *ticket_id*, or ``None`` if
        there is no open question.  The ``[ASK_USER]`` marker prefix is
        stripped.
        """
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            stmt = (
                select(Comment)
                .where(
                    Comment.ticket_id == ticket_id,
                    Comment.parent_id == None,  # noqa: E711
                    Comment.body.startswith(ASK_USER_MARKER),
                    Comment.closed_at == None,  # noqa: E711
                )
                .order_by(Comment.created_at.desc())  # type: ignore[attr-defined]
                .limit(1)
            )
            result = s.exec(stmt).first()
            if result is None:
                return None
            # Strip the marker prefix: f"{ASK_USER_MARKER}\n\n{question}"
            text = result.body
            if text.startswith(ASK_USER_MARKER):
                text = text[len(ASK_USER_MARKER) :]
            return text.lstrip("\n").strip()
