"""TicketService — the management-plane API surface over the DB.

All state mutation goes through here so the API, the worker, and tests
share one set of invariants (transition validation, history events,
workspace pointer upkeep). DB access is synchronous; the worker calls it
from its coroutine (never from the stage threadpool).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from collections.abc import Iterable
from datetime import datetime, timezone
from secrets import token_hex

from sqlmodel import select

from . import db
from ..config import Settings
from .models import Ticket, TicketEvent, Comment
from .states import State, can_transition
from .workspace import Workspace

log = logging.getLogger("robotsix_mill.service")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-")[:40] or "ticket"


def _parse_depends_on_str(raw: str | None) -> list[str]:
    """Parse a JSON-encoded list of ticket IDs from the depends_on
    column. Returns an empty list for ``None`` or malformed input."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return []


class TransitionError(RuntimeError):
    """Requested state transition is not allowed by the state machine."""


class TicketService:
    _ARCHIVABLE_STATES: set[State] = {State.CLOSED, State.ANSWERED, State.EPIC_CLOSED}

    def __init__(self, settings: Settings) -> None:
        """Create a service backed by the given :class:`Settings`.

        The settings provide the database path and workspace root directory.
        """
        self.settings = settings

    def workspace(self, ticket: Ticket) -> Workspace:
        """Return the :class:`Workspace` for *ticket*.

        Resolved from :attr:`Settings.workspaces_dir` and the ticket's ``id``.
        """
        return Workspace(self.settings.workspaces_dir, ticket.id)

    # --- reads ---
    def get(self, ticket_id: str) -> Ticket | None:
        """Look up a :class:`Ticket` by id, or return ``None``."""
        with db.session(self.settings) as s:
            return s.get(Ticket, ticket_id)

    def list(
        self,
        state: State | None = None,
        exclude_states: Iterable[State] | None = None,
    ) -> list[Ticket]:
        """List tickets, optionally filtered by *state* or excluding
        *exclude_states* (e.g. terminal CLOSED/DONE for a fast board).

        Results are ordered by ``created_at`` ascending.
        """
        with db.session(self.settings) as s:
            stmt = select(Ticket).order_by(Ticket.created_at)
            if state is not None:
                stmt = stmt.where(Ticket.state == state)
            if exclude_states:
                stmt = stmt.where(Ticket.state.notin_(list(exclude_states)))
            return list(s.exec(stmt).all())

    def history(self, ticket_id: str) -> list[TicketEvent]:
        """Return the :class:`TicketEvent` log for *ticket_id*, ordered by ``at``."""
        with db.session(self.settings) as s:
            stmt = (
                select(TicketEvent)
                .where(TicketEvent.ticket_id == ticket_id)
                .order_by(TicketEvent.at)
            )
            return list(s.exec(stmt).all())

    # --- writes ---
    def delete(self, ticket_id: str) -> bool:
        """Hard-delete a ticket: its row, its history events, and its
        workspace directory. Returns ``False`` if no such ticket.

        Irreversible — for purging junk / no-op tickets (e.g. a
        retrospect "no notable issues, clean run" draft). Safe even if
        the worker is mid-processing it: the next ``get()`` returns
        None and the worker treats it as a vanished ticket and stops.
        """
        with db.session(self.settings) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None:
                return False
            for ev in s.exec(
                select(TicketEvent).where(
                    TicketEvent.ticket_id == ticket_id
                )
            ).all():
                s.delete(ev)
            s.delete(ticket)
            s.commit()
        # Remove the workspace dir directly (don't construct Workspace —
        # its __init__ would recreate the directory).
        shutil.rmtree(
            self.settings.workspaces_dir / ticket_id, ignore_errors=True
        )
        # Remove the conversation file unconditionally.
        conv_file = self.settings.data_dir / "conversations" / f"{ticket_id}.json"
        try:
            conv_file.unlink(missing_ok=True)
        except OSError:
            pass
        return True

    def _maybe_purge_archived(self) -> None:
        """Purge oldest terminal tickets when the cap is exceeded.

        Reads ``max_archived_tickets`` from settings.  If <= 0 the
        purge is disabled.  Queries all tickets in ``_ARCHIVABLE_STATES``
        ordered by ``created_at`` ascending and deletes the oldest until
        the count is within the cap — but skips any terminal ticket that
        is the parent of at least one child in a non-archivable state.
        """
        max_archived = self.settings.max_archived_tickets
        if max_archived <= 0:
            return

        with db.session(self.settings) as s:
            stmt = (
                select(Ticket)
                .where(Ticket.state.in_(list(self._ARCHIVABLE_STATES)))
                .order_by(Ticket.created_at)
            )
            candidates = list(s.exec(stmt).all())

        if len(candidates) <= max_archived:
            return

        excess = len(candidates) - max_archived
        deleted = 0
        for ticket in candidates:
            if deleted >= excess:
                break
            # Skip if this terminal ticket is the parent of any
            # child still in a non-archivable (active) state.
            if self._has_active_child(ticket.id):
                continue
            self.delete(ticket.id)
            deleted += 1

    def _has_active_child(self, ticket_id: str) -> bool:
        """Return True if *ticket_id* has at least one child whose
        state is NOT in ``_ARCHIVABLE_STATES``."""
        with db.session(self.settings) as s:
            stmt = select(Ticket).where(
                Ticket.parent_id == ticket_id,
                Ticket.state.notin_(list(self._ARCHIVABLE_STATES)),
            ).limit(1)
            return s.exec(stmt).first() is not None

    def create(
        self, title: str, description: str = "", source: str = "user",
        origin_session: str | None = None,
        depends_on: str | None = None,
        kind: str = "task",
        parent_id: str | None = None,
    ) -> Ticket:
        """Create a new ticket with the given *title*.

        Side effects: creates a :class:`Workspace`, writes the optional
        *description* file, persists the :class:`Ticket` and a
        ``"created"`` :class:`TicketEvent`.

        The ticket id is constructed from the UTC timestamp, a slug of
        the title, and a short random hex suffix.

        When *kind* is ``"inquiry"`` the initial state is ``ASKED``
        (the answer stage picks it up) instead of ``DRAFT``.
        When *kind* is ``"epic"`` the initial state is ``EPIC_OPEN``.
        ``depends_on`` is NOT allowed for inquiries or epics — raises
        :class:`ValueError`.

        If *parent_id* is provided, the parent ticket must exist; the
        created ticket is linked to it via ``set_parent``.

        Raises :class:`ValueError` if *depends_on* includes the ticket's
        own ID (self-dependency), is provided for an inquiry or epic, or
        if *parent_id* references a nonexistent ticket.
        """
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        ticket_id = f"{stamp}-{_slug(title)}-{token_hex(2)}"

        if kind in ("inquiry", "epic") and depends_on:
            raise ValueError(
                f"{kind}s do not support depends_on — they are standalone"
            )

        # Reject self-dependency before persisting.
        if depends_on:
            dep_ids = _parse_depends_on_str(depends_on)
            if ticket_id in dep_ids:
                raise ValueError(
                    f"Ticket cannot depend on itself: {ticket_id}"
                )

        if kind == "epic":
            initial_state = State.EPIC_OPEN
        elif kind == "inquiry":
            initial_state = State.ASKED
        else:
            initial_state = State.DRAFT

        # Validate parent_id
        if parent_id is not None:
            with db.session(self.settings) as s:
                parent = s.get(Ticket, parent_id)
            if parent is None:
                raise ValueError(f"parent_id {parent_id!r} does not exist")

        ws = Workspace(self.settings.workspaces_dir, ticket_id)
        content_hash = ws.write_description(description)
        with db.session(self.settings) as s:
            ticket = Ticket(
                id=ticket_id,
                title=title,
                state=initial_state,
                kind=kind,
                workspace_path=str(ws.dir),
                content_hash=content_hash,
                source=source,
                origin_session=origin_session,
                depends_on=depends_on,
                parent_id=parent_id,
            )
            s.add(ticket)
            s.add(
                TicketEvent(
                    ticket_id=ticket_id, state=initial_state, note="created"
                )
            )
            s.commit()
            s.refresh(ticket)
            return ticket

    def transition(
        self, ticket_id: str, dst: State, note: str | None = None
    ) -> Ticket:
        """Move a ticket to *dst* state.

        Returns the updated :class:`Ticket`. Raises :class:`KeyError` if
        the ticket does not exist and :class:`TransitionError` if the
        transition is not allowed by the state machine.

        When transitioning to :class:`State.BLOCKED`, the originating
        state is recorded in ``blocked_from`` so it can be resumed later.
        """
        with db.session(self.settings) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None:
                raise KeyError(ticket_id)
            blocked_from = (
                State(ticket.blocked_from)
                if ticket.blocked_from
                else None
            )
            if not can_transition(ticket.state, dst, blocked_from):
                raise TransitionError(
                    f"{ticket_id}: {ticket.state} -> {dst} not allowed"
                )
            # Record originating state when blocking; clear when leaving
            # BLOCKED (regardless of resume or override path).
            if dst is State.BLOCKED:
                ticket.blocked_from = ticket.state.value
            elif ticket.state is State.BLOCKED:
                ticket.blocked_from = None
            ticket.state = dst
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.add(TicketEvent(ticket_id=ticket_id, state=dst, note=note))
            s.commit()
            s.refresh(ticket)
            # Purge oldest terminal tickets if we just crossed the cap.
            if dst in self._ARCHIVABLE_STATES:
                self._maybe_purge_archived()
            return ticket

    def resume_blocked(self, ticket_id: str) -> Ticket:
        """Resume a blocked ticket to the state it was blocked from.

        Reads ``ticket.blocked_from`` and transitions the ticket back to
        that state so only the failed stage is re-run.
        """
        with db.session(self.settings) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None:
                raise KeyError(ticket_id)
            if ticket.state is not State.BLOCKED:
                raise TransitionError(
                    f"{ticket_id}: cannot resume — not BLOCKED (currently {ticket.state})"
                )
            if not ticket.blocked_from:
                raise TransitionError(
                    f"{ticket_id}: cannot resume — no blocked_from recorded; "
                    "use a manual transition (READY or DRAFT) instead"
                )
            dst = State(ticket.blocked_from)
            if not can_transition(ticket.state, dst, dst):
                raise TransitionError(
                    f"{ticket_id}: {ticket.state} -> {dst} not allowed"
                )
            ticket.blocked_from = None
            ticket.state = dst
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.add(
                TicketEvent(
                    ticket_id=ticket_id,
                    state=dst,
                    note=f"resumed from blocked (was blocked from {dst.value})",
                )
            )
            s.commit()
            s.refresh(ticket)
            return ticket

    def set_branch(self, ticket_id: str, branch: str) -> None:
        """Record the git branch name for a ticket.

        Raises :class:`KeyError` if the ticket does not exist.
        """
        with db.session(self.settings) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None:
                raise KeyError(ticket_id)
            ticket.branch = branch
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.commit()

    def set_parent(self, ticket_id: str, parent_id: str) -> None:
        """Link a spawned ticket to the ticket it originated from
        (e.g. a retrospect improvement draft -> the reviewed ticket)."""
        with db.session(self.settings) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None:
                raise KeyError(ticket_id)
            ticket.parent_id = parent_id
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.commit()

    def get_epic_context(self, ticket: Ticket) -> str:
        """Return the epic description wrapped in ``<epic_context>`` tags
        if *ticket* has a parent whose ``kind`` is ``"epic"``, or ``""``
        otherwise."""
        if ticket.parent_id is None:
            return ""
        parent = self.get(ticket.parent_id)
        if parent is None or parent.kind != "epic":
            return ""
        desc = self.workspace(parent).read_description()
        if not desc:
            return ""
        return f"<epic_context>\n{desc}\n</epic_context>"

    def list_children(self, ticket_id: str) -> list[Ticket]:
        """Return all tickets whose ``parent_id`` equals *ticket_id*."""
        with db.session(self.settings) as s:
            stmt = select(Ticket).where(Ticket.parent_id == ticket_id)
            return list(s.exec(stmt).all())

    def cumulative_cost(
        self, ticket_id: str, settings: Settings, *, blocking: bool = True
    ) -> float:
        """Return the cumulative cost of *ticket_id* and all descendants (recursive).

        Uses the same blocking/cache-only mode as the caller — blocking
        for per-ticket detail views, cache-only for the polled /tickets list.
        """
        from ..langfuse_client import session_cost, session_cost_cached

        cost_fn = (
            (lambda sid: session_cost(settings, sid))
            if blocking
            else session_cost_cached
        )

        total = cost_fn(ticket_id)
        for descendant in self._all_descendants(ticket_id):
            total += cost_fn(descendant.id)
        return total

    def _all_descendants(self, ticket_id: str) -> list[Ticket]:
        """Return every descendant of *ticket_id* at any depth (BFS, cycle-safe)."""
        result: list[Ticket] = []
        visited: set[str] = {ticket_id}
        queue: list[str] = [ticket_id]
        with db.session(self.settings) as s:
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

    def set_title(self, ticket_id: str, title: str) -> None:
        """Update the title of a ticket. Raises :class:`KeyError` if
        the ticket does not exist."""
        with db.session(self.settings) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None:
                raise KeyError(ticket_id)
            ticket.title = title
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.commit()

    def set_content_hash(self, ticket_id: str, content_hash: str) -> None:
        """Keep the DB pointer in sync after a stage rewrites the
        file-canonical description (so it isn't seen as an external edit)."""
        with db.session(self.settings) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None:
                raise KeyError(ticket_id)
            ticket.content_hash = content_hash
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.commit()

    def set_depends_on(self, ticket_id: str, depends_on_ids: list[str]) -> None:
        """Set the ``depends_on`` field for *ticket_id* to a JSON-encoded
        list of ticket IDs.  Raises :class:`ValueError` if *ticket_id*
        appears in *depends_on_ids* (self-dependency)."""
        if ticket_id in depends_on_ids:
            raise ValueError(
                f"Ticket cannot depend on itself: {ticket_id}"
            )
        raw = json.dumps(depends_on_ids) if depends_on_ids else None
        with db.session(self.settings) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None:
                raise KeyError(ticket_id)
            ticket.depends_on = raw
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.commit()

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
                    ticket.id, dep_id,
                )
                continue

            # Direct cycle: A → B, B → A
            dep_deps = self._parse_depends_on(dep_ticket)
            if ticket.id in dep_deps:
                log.debug(
                    "ticket %s: direct cycle with dependency %s — treating as satisfied",
                    ticket.id, dep_id,
                )
                continue

            if dep_ticket.state in (State.CLOSED, State.DONE):
                continue

            unmet.append(dep_id)

        return unmet

    # --- comments ---
    def add_comment(self, ticket_id: str, body: str, author: str = "user") -> Comment:
        """Add a reviewer comment to a ticket. Raises ``KeyError`` if
        the ticket does not exist."""
        with db.session(self.settings) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None:
                raise KeyError(ticket_id)
            comment = Comment(ticket_id=ticket_id, body=body, author=author)
            s.add(comment)
            s.commit()
            s.refresh(comment)
            return comment

    def list_comments(self, ticket_id: str) -> list[Comment]:
        """Return all comments for *ticket_id*, ordered oldest-first.
        Raises ``KeyError`` if the ticket does not exist."""
        with db.session(self.settings) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None:
                raise KeyError(ticket_id)
            stmt = (
                select(Comment)
                .where(Comment.ticket_id == ticket_id)
                .order_by(Comment.created_at)
            )
            return list(s.exec(stmt).all())

    def request_changes(
        self, ticket_id: str, body: str, author: str = "user"
    ) -> tuple[Comment | None, Ticket]:
        """Transition from ``human_issue_approval`` to ``draft`` in one
        atomic operation.  When ``body`` is non-empty a ``Comment`` is
        also created.

        Returns the ``(Comment | None, Ticket)`` pair. Raises
        ``KeyError`` if the ticket does not exist, ``TransitionError``
        if it is not in ``human_issue_approval``.
        """
        with db.session(self.settings) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None:
                raise KeyError(ticket_id)
            if ticket.state is not State.HUMAN_ISSUE_APPROVAL:
                raise TransitionError(
                    f"{ticket_id}: cannot request changes — "
                    f"not human_issue_approval (currently {ticket.state})"
                )
            comment = None
            if body.strip():
                comment = Comment(ticket_id=ticket_id, body=body, author=author)
                s.add(comment)
            note = f"changes requested: {body}"
            ticket.state = State.DRAFT
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.add(
                TicketEvent(
                    ticket_id=ticket_id, state=State.DRAFT, note=note
                )
            )
            s.commit()
            if comment is not None:
                s.refresh(comment)
            s.refresh(ticket)
            return comment, ticket
