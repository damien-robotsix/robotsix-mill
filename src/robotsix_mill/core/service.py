"""TicketService — the management-plane API surface over the DB.

All state mutation goes through here so the API, the worker, and tests
share one set of invariants (transition validation, history events,
workspace pointer upkeep). DB access is synchronous; the worker calls it
from its coroutine (never from the stage threadpool).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timezone
from secrets import token_hex

from sqlmodel import select

from . import db
from ..config import Settings
from .models import (
    ActionType,
    Comment,
    ProposedAction,
    ProposedActionStatus,
    SourceKind,
    Ticket,
    TicketEvent,
)
from .states import State, can_transition
from .workspace import Workspace, prune_clone


def _event_hash(
    ticket_id: str,
    state: str,
    note: str | None,
    at: str,
    prev_hash: str | None,
) -> str:
    """Compute BLAKE2b hash over the canonical JSON payload of an event."""
    payload = {
        "ticket_id": ticket_id,
        "state": state,
        "note": note,
        "at": at,
        "prev_hash": prev_hash,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.blake2b(canonical.encode("utf-8"), digest_size=32).hexdigest()


def _prev_hash_for(db_session, ticket_id: str) -> str | None:
    """Return the hash of the most recent event for *ticket_id*, or None."""
    prev = db_session.exec(
        select(TicketEvent.hash)
        .where(TicketEvent.ticket_id == ticket_id)
        .order_by(TicketEvent.id.desc())
    ).first()
    return prev if prev else None


def _make_event(
    db_session,
    ticket_id: str,
    state: State,
    note: str | None = None,
) -> TicketEvent:
    """Build a TicketEvent with hash-chain fields populated."""
    at = datetime.now(timezone.utc)
    prev_hash = _prev_hash_for(db_session, ticket_id)
    h = _event_hash(
        ticket_id=ticket_id,
        state=state.value,
        note=note,
        at=at.isoformat(),
        prev_hash=prev_hash,
    )
    return TicketEvent(
        ticket_id=ticket_id,
        state=state,
        note=note,
        at=at,
        prev_hash=prev_hash,
        hash=h,
    )


log = logging.getLogger("robotsix_mill.service")

# A ticket auto-unblocks its ``unblocks`` targets when it reaches one of
# these completion states (DONE = merged/auto-merged; CLOSED = retrospected;
# EPIC_CLOSED = all epic children done). Firing on both DONE and CLOSED is
# idempotent — targets are only moved if still BLOCKED.
_UNBLOCK_TRIGGER_STATES: set[State] = {
    State.DONE,
    State.CLOSED,
    State.EPIC_CLOSED,
}

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
    except json.JSONDecodeError, TypeError:
        pass
    return []


def _parse_labels(raw: str | None) -> list[str]:
    """Parse a JSON-encoded list of label strings from the labels
    column. Returns an empty list for ``None`` or malformed input."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
            return parsed
    except json.JSONDecodeError, TypeError:
        pass
    return []


class TransitionError(RuntimeError):
    """Requested state transition is not allowed by the state machine."""


class TicketService:
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
    * **Proposed actions** — :meth:`create_proposed_action`,
      :meth:`approve_proposed_action`, :meth:`reject_proposed_action`,
      :meth:`execute_proposed_action`.
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
            return self._get_anywhere(ticket_id)
        with db.session(self.settings, self.board_id) as s:
            ticket = s.get(Ticket, ticket_id)
        if ticket is not None:
            self._resolve_board_id(ticket)
            return ticket
        # Bound-board miss — fall back to fanout. With per-repo DBs
        # the worker's & routes' default service is pinned to the
        # first repo, so any ticket in another repo's DB would 404
        # without this fallback.
        return self._get_anywhere(ticket_id)

    def _get_anywhere(self, ticket_id: str) -> Ticket | None:
        """Search every per-repo DB for *ticket_id*. Ticket IDs are
        globally unique so the first hit is the answer.

        Discovers candidate boards two ways so we don't miss any:
        1. From the registered :class:`ReposRegistry` (production path
           — repos.yaml configured).
        2. By scanning ``data_dir`` for ``<board>/mill.db`` files
           (robust to test setups that don't register repos but write
           per-board DBs via the migration / direct-board service).
        """
        from ..config import get_repos_config

        candidates: list[str] = []
        try:
            for rc in get_repos_config().repos.values():
                if rc.board_id and rc.board_id not in candidates:
                    candidates.append(rc.board_id)
        except Exception:
            pass
        # Disk-scan fallback for boards not in the registry.
        try:
            for sub in self.settings.data_dir.iterdir():
                if sub.is_dir() and (sub / "mill.db").exists():
                    if sub.name not in candidates:
                        candidates.append(sub.name)
        except OSError:
            pass
        for board_id in candidates:
            with db.session(self.settings, board_id) as s:
                ticket = s.get(Ticket, ticket_id)
                if ticket is not None:
                    self._resolve_board_id(ticket)
                    return ticket
        return None

    def list(
        self,
        state: State | None = None,
        exclude_states: Iterable[State] | None = None,
    ) -> list[Ticket]:
        """List tickets, optionally filtered by *state* or excluding
        *exclude_states* (e.g. terminal CLOSED/DONE for a fast board).

        Results are ordered by ``created_at`` ascending.
        """
        with db.session(self.settings, self.board_id) as s:
            stmt = select(Ticket).order_by(Ticket.created_at)
            if state is not None:
                stmt = stmt.where(Ticket.state == state)
            if exclude_states:
                stmt = stmt.where(Ticket.state.notin_(list(exclude_states)))
            tickets = list(s.exec(stmt).all())
        for t in tickets:
            self._resolve_board_id(t)
        return tickets

    def _board_for(self, ticket_id: str) -> str:
        """Resolve the actual board that holds *ticket_id*.

        Returns ``self.board_id`` when the bound DB has the row, else
        fans out via ``_get_anywhere`` and returns the discovered
        board. Falls back to ``self.board_id`` (which may be empty)
        when the ticket is not found anywhere — callers then operate
        on the default DB and the row will simply not exist there.
        """
        if self.board_id:
            with db.session(self.settings, self.board_id) as s:
                if s.get(Ticket, ticket_id) is not None:
                    return self.board_id
        t = self._get_anywhere(ticket_id)
        return (t.board_id if t and t.board_id else self.board_id) or ""

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

    # --- proposed actions ---

    def create_proposed_action(
        self,
        source: str,
        target_ticket_id: str,
        action_type: str,
        rationale: str,
        payload: str | None = None,
    ) -> ProposedAction | None:
        """Create a ``ProposedAction`` row with status ``PENDING``.

        Validates *action_type* against :class:`ActionType`.  On
        invalid action_type or FK violation (non-existent target
        ticket), logs a warning and returns ``None`` — never raises
        for a single bad proposal, so one failure doesn't crash the
        whole pass.
        """
        try:
            ActionType(action_type)
        except ValueError:
            log.warning(
                "create_proposed_action: invalid action_type %r — skipping",
                action_type,
            )
            return None

        try:
            with db.session(self.settings, self.board_id) as s:
                pa = ProposedAction(
                    source=source,
                    target_ticket_id=target_ticket_id,
                    action_type=ActionType(action_type),
                    payload=payload,
                    rationale=rationale,
                    status=ProposedActionStatus.PENDING,
                )
                s.add(pa)
                s.commit()
                s.refresh(pa)
                return pa
        except Exception:
            log.warning(
                "create_proposed_action: failed to persist proposal "
                "(%s on %s) — target ticket may not exist",
                action_type,
                target_ticket_id,
                exc_info=True,
            )
            return None

    def list_proposed_actions(
        self,
        source: str | None = None,
        *,
        status: ProposedActionStatus | None = None,
        exclude_status: ProposedActionStatus | None = ProposedActionStatus.PENDING,
    ) -> list[ProposedAction]:
        """Return ``ProposedAction`` rows on this board, newest first.

        Three optional, orthogonal filters:

        * ``source`` — restrict to one producing agent label.
        * ``status`` — keep ONLY rows in this status.
        * ``exclude_status`` — drop rows in this status; **defaults to
          ``PENDING``** so the pass-runner verification path
          (``list_proposed_actions(source=...)``) gets decided rows only.
          The GET ``/proposed-actions?status=`` route passes
          ``exclude_status=None`` to filter purely by ``status``.
        """
        with db.session(self.settings, self.board_id) as s:
            stmt = select(ProposedAction)
            if source is not None:
                stmt = stmt.where(ProposedAction.source == source)
            if status is not None:
                stmt = stmt.where(ProposedAction.status == status)
            if exclude_status is not None:
                stmt = stmt.where(ProposedAction.status != exclude_status)
            stmt = stmt.order_by(ProposedAction.created_at.desc())
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
        board = self._board_for(ticket_id)
        with db.session(self.settings, board) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None:
                return False
            for ev in s.exec(
                select(TicketEvent).where(TicketEvent.ticket_id == ticket_id)
            ).all():
                s.delete(ev)
            s.delete(ticket)
            s.commit()
        # Remove the workspace dir directly (don't construct Workspace —
        # its __init__ would recreate the directory). Route via the
        # per-repo workspaces dir.
        shutil.rmtree(
            self.settings.workspaces_dir_for(board) / ticket_id,
            ignore_errors=True,
        )
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

        with db.session(self.settings, self.board_id) as s:
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
        with db.session(self.settings, self.board_id) as s:
            stmt = (
                select(Ticket)
                .where(
                    Ticket.parent_id == ticket_id,
                    Ticket.state.notin_(list(self._ARCHIVABLE_STATES)),
                )
                .limit(1)
            )
            return s.exec(stmt).first() is not None

    def create(
        self,
        title: str,
        description: str = "",
        source: str = SourceKind.USER,
        origin_session: str | None = None,
        depends_on: str | None = None,
        unblocks: str | None = None,
        kind: str = "task",
        parent_id: str | None = None,
        board_id: str | None = None,
        priority: bool = False,
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

        *board_id* overrides ``self.board_id`` when provided — used by
        the multi-repo API surface to stamp the correct board on each
        ticket.

        Raises :class:`ValueError` if *depends_on* includes the ticket's
        own ID (self-dependency), is provided for an inquiry or epic, or
        if *parent_id* references a nonexistent ticket.
        """
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        ticket_id = f"{stamp}-{_slug(title)}-{token_hex(2)}"

        if kind in ("inquiry", "epic") and depends_on:
            raise ValueError(f"{kind}s do not support depends_on — they are standalone")

        # Reject self-dependency before persisting.
        if depends_on:
            dep_ids = _parse_depends_on_str(depends_on)
            if ticket_id in dep_ids:
                raise ValueError(f"Ticket cannot depend on itself: {ticket_id}")

        if kind == "epic":
            initial_state = State.EPIC_OPEN
        elif kind == "inquiry":
            initial_state = State.ASKED
        else:
            initial_state = State.DRAFT

        # Route to the right per-repo DB / workspace: use the
        # explicit board_id override when provided (the route
        # creates a ticket for a different repo than this service
        # is bound to), else self.board_id.
        effective_board = board_id if board_id is not None else self.board_id

        # In multi-repo mode every ticket MUST belong to a board —
        # otherwise it ends up in the default mill.db and the UI
        # can't find it (the per-repo list endpoints filter by
        # board_id). Reject board-less creates so an agent tool
        # that forgot to thread board_id raises here instead of
        # silently producing an orphan ticket + an orphan
        # ``.data/workspaces/<id>`` directory.
        if not effective_board:
            from ..config import get_repos_config

            try:
                repos = get_repos_config().repos
            except Exception:
                repos = {}
            if repos and not self.settings.default_repo_id:
                raise ValueError(
                    "refusing to create board-less ticket in multi-repo "
                    "mode: pass an explicit board_id, or configure "
                    "MILL_DEFAULT_REPO_ID. "
                    f"(title={title!r}, source={source!r})"
                )

        # Validate parent_id against the EFFECTIVE board's DB.
        if parent_id is not None:
            with db.session(self.settings, effective_board) as s:
                parent = s.get(Ticket, parent_id)
            if parent is None:
                raise ValueError(f"parent_id {parent_id!r} does not exist")

        ws = Workspace(self.settings.workspaces_dir_for(effective_board), ticket_id)
        content_hash = ws.write_description(description)
        # Inherit priority from any priority-marked ancestor at
        # creation time. set_priority on an epic propagates to
        # CURRENT children; this walk catches children created AFTER
        # the epic was flagged. Loop is bounded by parent-chain depth
        # and skips cycles (which shouldn't exist but cheap to guard).
        inherited_priority = False
        if parent_id is not None:
            seen: set[str] = set()
            cur = parent_id
            while cur and cur not in seen:
                seen.add(cur)
                with db.session(self.settings, effective_board) as s:
                    p = s.get(Ticket, cur)
                if p is None:
                    break
                if getattr(p, "priority", False):
                    inherited_priority = True
                    break
                cur = p.parent_id
        with db.session(self.settings, effective_board) as s:
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
                unblocks=unblocks,
                parent_id=parent_id,
                board_id=board_id if board_id is not None else self.board_id,
                priority=priority or inherited_priority,
            )
            s.add(ticket)
            s.flush()
            s.add(
                _make_event(s, ticket_id=ticket_id, state=initial_state, note="created")
            )
            s.commit()
            s.refresh(ticket)
            return ticket

    def add_step_event(
        self,
        ticket_id: str,
        note: str,
    ) -> None:
        """Append a same-state event to a ticket's history.

        For agent conclusions that don't change state — scope-triage
        EXPAND continues the implement loop, doc-classifier verdict
        leaves the stage running. Those used to be emitted as
        comments so the UI showed them; they now live in history so
        comments stay reserved for human/agent interaction (ASK_USER,
        code review threads).

        The event carries the ticket's CURRENT state and the
        ``note`` describing what the agent concluded. The hash chain
        is extended like any other event.
        """
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None:
                raise KeyError(ticket_id)
            s.add(
                _make_event(
                    s,
                    ticket_id=ticket_id,
                    state=ticket.state,
                    note=note,
                )
            )
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.commit()

    def transition(self, ticket_id: str, dst: State, note: str | None = None) -> Ticket:
        """Move a ticket to *dst* state.

        Returns the updated :class:`Ticket`. Raises :class:`KeyError` if
        the ticket does not exist and :class:`TransitionError` if the
        transition is not allowed by the state machine.

        When transitioning to :class:`State.BLOCKED`, the originating
        state is recorded in ``blocked_from`` so it can be resumed later.
        """
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None:
                raise KeyError(ticket_id)
            blocked_from = State(ticket.blocked_from) if ticket.blocked_from else None
            paused_from = State(ticket.paused_from) if ticket.paused_from else None
            if not can_transition(ticket.state, dst, blocked_from, paused_from):
                raise TransitionError(
                    f"{ticket_id}: {ticket.state} -> {dst} not allowed"
                )
            # Record originating state when blocking; clear when leaving
            # BLOCKED (regardless of resume or override path).
            if dst is State.BLOCKED:
                ticket.blocked_from = ticket.state.value
            elif ticket.state is State.BLOCKED:
                ticket.blocked_from = None
            # Record originating state when pausing mid-stage; clear when
            # leaving AWAITING_USER_REPLY (resume path).
            if dst is State.AWAITING_USER_REPLY:
                ticket.paused_from = ticket.state.value
            elif ticket.state is State.AWAITING_USER_REPLY:
                ticket.paused_from = None
            ticket.state = dst
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.flush()
            s.add(_make_event(s, ticket_id=ticket_id, state=dst, note=note))
            s.commit()
            s.refresh(ticket)
            # Purge oldest terminal tickets if we just crossed the cap.
            if dst in self._ARCHIVABLE_STATES:
                self._maybe_purge_archived()
            if self._on_transition is not None:
                self._on_transition(ticket)
            # Capture unblock targets to fire AFTER this session closes
            # (cross-board: each target may live on another board's DB; we
            # must not hold this session open while transitioning them).
            unblock_targets = (
                _parse_depends_on_str(ticket.unblocks)
                if dst in _UNBLOCK_TRIGGER_STATES
                else []
            )
        if unblock_targets:
            self._fire_unblocks(ticket_id, unblock_targets)
        return self.get(ticket_id) or ticket

    def _fire_unblocks(self, solver_id: str, target_ids: list[str]) -> None:
        """Transition each BLOCKED ticket in *target_ids* to DRAFT.

        Called when *solver_id* completes. Best-effort and idempotent: a
        target that is missing or not currently BLOCKED is skipped (so
        re-firing on DONE then CLOSED is a no-op the second time). Targets
        may live on other boards — ``transition`` resolves each via
        ``_board_for``.
        """
        note = f"auto-unblocked: solver {solver_id} completed"
        for tid in target_ids:
            try:
                target = self.get(tid)
                if target is None or target.state is not State.BLOCKED:
                    continue
                self.transition(tid, State.DRAFT, note=note)
                log.info("unblock: %s -> DRAFT (solver %s completed)", tid, solver_id)
            except Exception:
                log.warning(
                    "unblock: failed to re-open %s (solver %s)",
                    tid,
                    solver_id,
                    exc_info=True,
                )

    def set_unblocks(self, ticket_id: str, target_ids: list[str]) -> Ticket:
        """Set the list of ticket IDs *ticket_id* auto-unblocks on completion.

        Stored as a JSON array; replaces any prior value. Self-references are
        dropped. Returns the updated ticket; raises ``KeyError`` if unknown.
        """
        cleaned = [t for t in dict.fromkeys(target_ids) if t and t != ticket_id]
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None:
                raise KeyError(ticket_id)
            ticket.unblocks = json.dumps(cleaned) if cleaned else None
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.commit()
            s.refresh(ticket)
            return ticket

    def set_labels(self, ticket_id: str, labels: list[str]) -> Ticket:
        """Set the free-form label list applied to *ticket_id*.

        Stored as a JSON array; replaces any prior value. Duplicates are
        dropped preserving order; an empty list is stored as ``None``.
        Returns the updated ticket; raises ``KeyError`` if unknown.
        """
        cleaned: list[str] = list(dict.fromkeys(labels))
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None:
                raise KeyError(ticket_id)
            ticket.labels = json.dumps(cleaned) if cleaned else None
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.commit()
            s.refresh(ticket)
            return ticket

    def add_history_note(self, ticket_id: str, note: str) -> TicketEvent:
        """Append a non-transition history entry that records an
        informational note on the ticket.

        Used for the post-stage Langfuse trace breadcrumb. Previously
        the worker posted that link as a comment (author=mill); refine
        and implement then read the comment stream and treated the
        inaccessible URL as reviewer feedback. Writing to history
        instead keeps the audit trail visible to a human browsing the
        ticket without contaminating the channel agents read.

        The event reuses the ticket's CURRENT state — it's a side-band
        note, not a transition. Hash chain stays intact: the next real
        transition's ``prev_hash`` correctly points at this entry.
        """
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None:
                raise KeyError(ticket_id)
            event = _make_event(s, ticket_id=ticket_id, state=ticket.state, note=note)
            s.add(event)
            s.commit()
            s.refresh(event)
            return event

    def resume_blocked(self, ticket_id: str) -> Ticket:
        """Resume a blocked ticket to the state it was blocked from.

        Reads ``ticket.blocked_from`` and transitions the ticket back to
        that state so only the failed stage is re-run.
        """
        with db.session(self.settings, self._board_for(ticket_id)) as s:
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
            ticket.retry_attempt = 0
            ticket.last_transient_error = None
            ticket.next_retry_at = None
            ticket.state = dst
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.flush()
            s.add(
                _make_event(
                    s,
                    ticket_id=ticket_id,
                    state=dst,
                    note=f"resumed from blocked (was blocked from {dst.value})",
                )
            )
            s.commit()
            s.refresh(ticket)
            if self._on_transition is not None:
                self._on_transition(ticket)
            return ticket

    def set_retry_state(
        self,
        ticket_id: str,
        *,
        retry_attempt: int,
        last_transient_error: str | None,
        next_retry_at: datetime | None,
    ) -> None:
        """Set transient-error retry metadata on a ticket.

        Does NOT create a ``TicketEvent`` — the workflow state hasn't changed.
        """
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None:
                raise KeyError(ticket_id)
            ticket.retry_attempt = retry_attempt
            ticket.last_transient_error = last_transient_error
            ticket.next_retry_at = next_retry_at
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.commit()

    def set_priority(self, ticket_id: str, priority: bool) -> list[str]:
        """Toggle the operator-controlled priority flag on a ticket.

        When True, the worker pulls this ticket off the queue ahead of
        non-priority tickets — used to jump bug-fix tickets in front of
        the normal backlog without changing dependency wiring.

        Epic propagation: when the target ticket has descendants (epic
        with children, sub-epics, etc.) the flag is applied to every
        descendant too. Children created LATER also inherit the
        priority via the create-time parent-chain walk (see
        :meth:`create`). Returns the list of ticket IDs whose priority
        was changed (the target plus any affected descendants) so the
        caller can re-enqueue each one.
        """
        changed: list[str] = []
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None:
                raise KeyError(ticket_id)
            new_value = bool(priority)
            if ticket.priority != new_value:
                ticket.priority = new_value
                ticket.updated_at = datetime.now(timezone.utc)
                s.add(ticket)
                changed.append(ticket.id)
            s.commit()
        # Propagate to every descendant. _all_descendants walks the
        # parent_id graph and is cycle-safe.
        for descendant in self._all_descendants(ticket_id):
            with db.session(self.settings, self._board_for(descendant.id)) as s:
                d = s.get(Ticket, descendant.id)
                if d is None or d.priority == bool(priority):
                    continue
                d.priority = bool(priority)
                d.updated_at = datetime.now(timezone.utc)
                s.add(d)
                s.commit()
                changed.append(d.id)
        return changed

    def set_branch(self, ticket_id: str, branch: str) -> None:
        """Record the git branch name for a ticket.

        Raises :class:`KeyError` if the ticket does not exist.
        """
        with db.session(self.settings, self._board_for(ticket_id)) as s:
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
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None:
                raise KeyError(ticket_id)
            ticket.parent_id = parent_id
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.commit()

    def get_epic_context(self, ticket: Ticket) -> str:
        """Return the epic description wrapped in an ``epic-context``
        fenced block if *ticket* has a parent whose ``kind`` is
        ``"epic"``, or ``""`` otherwise."""
        if ticket.parent_id is None:
            return ""
        parent = self.get(ticket.parent_id)
        if parent is None or parent.kind != "epic":
            return ""
        desc = self.workspace(parent).read_description()
        if not desc:
            return ""
        from ..agents.prompt_blocks import section

        return section("epic-context", desc)

    def list_children(self, ticket_id: str) -> list[Ticket]:
        """Return all tickets whose ``parent_id`` equals *ticket_id*."""
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            stmt = select(Ticket).where(Ticket.parent_id == ticket_id)
            return list(s.exec(stmt).all())

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
        from ..langfuse.client import session_cost, session_cost_cached

        cost_fn = (
            (lambda sid: session_cost(settings, sid, repo_config=repo_config))
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

    def set_title(self, ticket_id: str, title: str) -> None:
        """Update the title of a ticket. Raises :class:`KeyError` if
        the ticket does not exist."""
        with db.session(self.settings, self._board_for(ticket_id)) as s:
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
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None:
                raise KeyError(ticket_id)
            ticket.content_hash = content_hash
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.commit()

    def promote_to_epic(self, ticket_id: str) -> None:
        """Flip a task ticket's kind to ``epic`` without changing state.

        Used by the refine stage's ``promote_to_epic`` path: refine flips
        the kind here, then the stage returns ``Outcome(EPIC_OPEN, …)``
        and the worker performs the actual state transition through the
        standard ``transition()`` path (which writes the state event).

        No-op for tickets already kind=epic. Raises ``KeyError`` for
        unknown ids.
        """
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None:
                raise KeyError(ticket_id)
            if ticket.kind == "epic":
                return
            ticket.kind = "epic"
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.commit()

    def set_review_rounds(self, ticket_id: str, value: int) -> None:
        """Set the ``review_rounds`` counter on *ticket_id*."""
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None:
                raise KeyError(ticket_id)
            ticket.review_rounds = value
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.commit()

    def set_depends_on(self, ticket_id: str, depends_on_ids: list[str]) -> None:
        """Set the ``depends_on`` field for *ticket_id* to a JSON-encoded
        list of ticket IDs.  Raises :class:`ValueError` if *ticket_id*
        appears in *depends_on_ids* (self-dependency)."""
        if ticket_id in depends_on_ids:
            raise ValueError(f"Ticket cannot depend on itself: {ticket_id}")
        raw = json.dumps(depends_on_ids) if depends_on_ids else None
        with db.session(self.settings, self._board_for(ticket_id)) as s:
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
    def add_comment(
        self,
        ticket_id: str,
        body: str,
        author: str = "user",
        parent_id: int | None = None,
    ) -> Comment:
        """Add a reviewer comment to a ticket. Raises ``KeyError`` if
        the ticket does not exist.

        When *parent_id* is given, validates that the parent Comment
        exists and belongs to the same ticket, raising ``ValueError``
        otherwise."""
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None:
                raise KeyError(ticket_id)
            if parent_id is not None:
                parent = s.get(Comment, parent_id)
                if parent is None:
                    raise ValueError(f"parent comment {parent_id} not found")
                if parent.ticket_id != ticket_id:
                    raise ValueError(
                        f"parent comment {parent_id} does not belong to ticket {ticket_id}"
                    )
            comment = Comment(
                ticket_id=ticket_id, body=body, author=author, parent_id=parent_id
            )
            s.add(comment)
            s.commit()
            s.refresh(comment)
            return comment

    def list_comments(self, ticket_id: str) -> list[Comment]:
        """Return all comments for *ticket_id*, ordered oldest-first.
        Raises ``KeyError`` if the ticket does not exist."""
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None:
                raise KeyError(ticket_id)
            stmt = (
                select(Comment)
                .where(Comment.ticket_id == ticket_id)
                .order_by(Comment.created_at)
            )
            return list(s.exec(stmt).all())

    def _board_for_comment(
        self,
        comment_id: int,
        ticket_id: str | None = None,
    ) -> str:
        """Resolve the board that owns *comment_id*.

        ``Comment.id`` is per-board auto-increment (each repo's
        SQLite assigns its own integer sequence), so a bare comment
        id is ambiguous across boards. When *ticket_id* is provided
        the lookup is unambiguous — the comment lives on the same
        board as its ticket. The route handlers always have the
        ticket id in hand (the user is on a ticket page when closing
        a thread), so this is the production path.

        Fall back to a cross-board fanout when *ticket_id* is missing,
        purely for backward compatibility with callers that haven't
        been threaded through yet. The fanout picks the first board
        whose DB contains a matching id — wrong on collisions, but
        no worse than the prior behaviour.
        """
        if ticket_id is not None:
            return self._board_for(ticket_id)

        from ..config import get_repos_config

        candidates: list[str] = [self.board_id]

        try:
            for rc in get_repos_config().repos.values():
                if rc.board_id and rc.board_id not in candidates:
                    candidates.append(rc.board_id)
        except Exception:
            pass
        try:
            for sub in self.settings.data_dir.iterdir():
                if sub.is_dir() and (sub / "mill.db").exists():
                    if sub.name not in candidates:
                        candidates.append(sub.name)
        except OSError:
            pass
        for board_id in candidates:
            with db.session(self.settings, board_id) as s:
                if s.get(Comment, comment_id) is not None:
                    return board_id
        return self.board_id

    def close_thread(
        self,
        comment_id: int,
        ticket_id: str | None = None,
    ) -> Comment:
        """Close a top-level comment thread.  Raises ``KeyError`` if
        the comment does not exist, ``ValueError`` if it is a reply
        (non-NULL parent_id) or is already closed.

        When the closed thread was an ``[ASK_USER]`` question on a
        ticket in ``AWAITING_USER_REPLY``, and every other
        ``[ASK_USER]`` thread on that ticket is also closed, the ticket
        is automatically resumed to its pre-pause state.

        *ticket_id* disambiguates the board in multi-repo mode (
        ``Comment.id`` is per-board, not globally unique). When the
        caller has the ticket id in hand (e.g. from the UI / agent
        tool) it MUST be passed — without it the lookup falls back
        to a cross-board fanout that picks the first board whose
        SQLite happens to have a matching id, which is the wrong
        comment on a collision.
        """
        board = self._board_for_comment(comment_id, ticket_id)
        with db.session(self.settings, board) as s:
            comment = s.get(Comment, comment_id)
            if comment is None:
                raise KeyError(comment_id)
            if comment.parent_id is not None:
                raise ValueError("only top-level threads can be closed")
            if comment.closed_at is not None:
                raise ValueError("thread already closed")
            comment.closed_at = datetime.now(timezone.utc)
            s.add(comment)
            ticket_id = comment.ticket_id
            s.commit()
            s.refresh(comment)

        # Post-close: auto-resume if all [ASK_USER] threads on a paused
        # ticket are now closed.  Use the SAME board (and a fresh
        # session) so the commit above is visible.
        self._maybe_resume_awaiting_user_reply(ticket_id, board)

        return comment

    def _maybe_resume_awaiting_user_reply(
        self,
        ticket_id: str,
        board: str,
    ) -> None:
        """If *ticket_id* is in ``AWAITING_USER_REPLY`` and every
        top-level ``[ASK_USER]`` comment thread on it is closed,
        transition the ticket back to its ``paused_from`` state."""
        with db.session(self.settings, board) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None or ticket.state is not State.AWAITING_USER_REPLY:
                return

            if not ticket.paused_from:
                log.warning(
                    "%s: AWAITING_USER_REPLY but no paused_from — cannot auto-resume",
                    ticket_id,
                )
                return

            # Count all top-level [ASK_USER] threads and check whether
            # every one is closed.
            stmt = select(Comment).where(
                Comment.ticket_id == ticket_id,
                Comment.parent_id == None,  # noqa: E711 (SQLAlchemy needs == None for SQL IS NULL)
                Comment.body.startswith("[ASK_USER]"),
            )
            ask_threads = list(s.exec(stmt).all())

            # No [ASK_USER] threads at all → skip (shouldn't happen on a
            # legitimately paused ticket, but be defensive).
            if not ask_threads:
                return

            if any(t.closed_at is None for t in ask_threads):
                return  # at least one still open

            # All [ASK_USER] threads closed → resume.
            dst = State(ticket.paused_from)
            ticket.blocked_from = None
            ticket.paused_from = None
            ticket.state = dst
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.flush()
            s.add(
                _make_event(
                    s,
                    ticket_id=ticket_id,
                    state=dst,
                    note="all ask_user threads closed — resuming",
                )
            )
            s.commit()
            s.refresh(ticket)
            log.info(
                "%s: auto-resumed from AWAITING_USER_REPLY → %s "
                "(all %d ask_user threads closed)",
                ticket_id,
                dst.value,
                len(ask_threads),
            )
            if self._on_transition is not None:
                self._on_transition(ticket)

    def reopen_thread(
        self,
        comment_id: int,
        ticket_id: str | None = None,
    ) -> Comment:
        """Reopen a closed top-level comment thread.  Raises
        ``KeyError`` if the comment does not exist, ``ValueError`` if
        it is a reply (non-NULL parent_id) or is not currently closed."""
        with db.session(
            self.settings, self._board_for_comment(comment_id, ticket_id)
        ) as s:
            comment = s.get(Comment, comment_id)
            if comment is None:
                raise KeyError(comment_id)
            if comment.parent_id is not None:
                raise ValueError("only top-level threads can be reopened")
            if comment.closed_at is None:
                raise ValueError("thread is not closed")
            comment.closed_at = None
            s.add(comment)
            s.commit()
            s.refresh(comment)
            return comment

    def redraft(
        self, ticket_id: str, body: str = "", author: str = "user"
    ) -> tuple[Comment | None, Ticket]:
        """Redraft a ticket from any active state — a clean-slate reset
        back to DRAFT.

        Unlike a plain back-to-draft transition, redraft *really starts
        the ticket over from scratch*: it folds the current description,
        all comments, and the optional redraft *body* into a single
        fresh ``description.md``; deletes the comment thread; drops all
        prior ``TicketEvent`` rows so the new DRAFT event is the genesis
        of a fresh hash chain; prunes the per-ticket repo clone (which
        holds the local implement branch); clears ``ticket.branch``; and
        snapshots the current full Langfuse session cost into
        ``ticket.pre_redraft_cost_usd`` (zeroing the cached
        ``ticket.cost_usd``) so the effective per-attempt cost —
        ``max(0.0, session_total - pre_redraft_cost_usd)`` — restarts at
        zero for the dollar-cap limit while the full total stays
        available for informational display.

        Note: only the *local* clone/branch and the ``ticket.branch`` DB
        pointer are cleared. The pushed remote branch and any open PR on
        the forge are left untouched — there is no remote-branch-delete
        helper and doing so would need network + forge API access.

        The returned ``Comment`` is always ``None`` (the redraft reason
        is folded into the body, not kept as a standalone comment).

        Raises :class:`KeyError` if the ticket does not exist,
        :class:`TransitionError` if it is already DRAFT or in a
        terminal state (CLOSED, ANSWERED, EPIC_CLOSED) or is an
        EPIC_OPEN epic.
        """
        _NON_REDRAFTABLE: set[State] = {
            State.DRAFT,
            State.CLOSED,
            State.ANSWERED,
            State.EPIC_CLOSED,
            State.EPIC_OPEN,
        }
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None:
                raise KeyError(ticket_id)
            if ticket.state in _NON_REDRAFTABLE:
                raise TransitionError(
                    f"{ticket_id}: cannot redraft — "
                    f"state {ticket.state} is not eligible for redraft"
                )

            # --- compact issue + comments into a clean body ---
            ws = self.workspace(ticket)
            original = ws.read_description()
            comments = list(
                s.exec(
                    select(Comment)
                    .where(Comment.ticket_id == ticket_id)
                    .order_by(Comment.created_at)
                ).all()
            )
            folded: list[str] = []
            if body.strip():
                folded.append(body)
            for c in comments:
                folded.append(f"**{c.author}** — {c.created_at.isoformat()}:\n{c.body}")
            if folded:
                new_body = (
                    f"{original}\n\n---\n## Folded-in on redraft\n"
                    + "\n\n".join(folded)
                )
            else:
                new_body = original
            ticket.content_hash = ws.write_description(new_body)

            # --- delete the comment thread ---
            for c in comments:
                s.delete(c)

            # --- delete ticket history so the DRAFT event below becomes
            # the genesis of a fresh hash chain (prev_hash is None) ---
            for ev in s.exec(
                select(TicketEvent).where(TicketEvent.ticket_id == ticket_id)
            ).all():
                s.delete(ev)
            s.flush()

            # --- delete the local workspace clone/branch ---
            # Only the LOCAL clone (repo/, which holds the implement
            # branch) and the ticket.branch DB pointer are cleared. The
            # pushed remote branch / open PR are NOT touched — there is
            # no remote-branch-delete helper and it would need network +
            # forge API access.
            prune_clone(ws)
            shutil.rmtree(ws.dir / "artifacts", ignore_errors=True)
            ticket.branch = None
            # Clean slate also means a fresh cost ledger — the
            # accumulated cost of the prior (discarded) attempt must not
            # carry over into the redrafted ticket. The Langfuse session
            # total is cumulative over the session's whole lifetime and
            # cannot be cleared locally, so snapshot it as a baseline:
            # the effective per-attempt cost subtracts this baseline so
            # the dollar-cap limit restarts at zero. A forced
            # (TTL-bypassing) read keeps the snapshot fresh; an
            # unconfigured/unreachable Langfuse returns 0.0, the correct
            # no-op baseline. ``repo_config`` is not available here, so
            # the global ``Secrets`` fallback is used (as in
            # ``cumulative_cost``).
            from ..langfuse.client import session_cost

            ticket.pre_redraft_cost_usd = session_cost(
                self.settings, ticket_id, force=True
            )
            ticket.cost_usd = 0.0

            note = f"redrafted: {body}" if body else "redrafted"
            ticket.state = State.DRAFT
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.flush()
            s.add(_make_event(s, ticket_id=ticket_id, state=State.DRAFT, note=note))
            s.commit()
            s.refresh(ticket)
            if self._on_transition is not None:
                self._on_transition(ticket)
            return None, ticket

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
        with db.session(self.settings, self._board_for(ticket_id)) as s:
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
            s.flush()
            s.add(_make_event(s, ticket_id=ticket_id, state=State.DRAFT, note=note))
            s.commit()
            if comment is not None:
                s.refresh(comment)
            s.refresh(ticket)
            if self._on_transition is not None:
                self._on_transition(ticket)
            return comment, ticket

    def mark_done(
        self, ticket_id: str, note: str = "", author: str = "user"
    ) -> tuple[Comment | None, Ticket]:
        """Mark a ticket as DONE from any non-terminal state.

        This is an escape hatch that bypasses ``can_transition()`` —
        similar to ``redraft()`` and ``request_changes()``.  Terminal
        states (DONE, CLOSED, ANSWERED, EPIC_CLOSED) and EPIC_OPEN are
        rejected.

        Returns ``(Comment | None, Ticket)``.  Raises ``KeyError`` if
        the ticket does not exist, ``TransitionError`` if the state is
        not eligible.
        """
        _NON_MARK_DONEABLE: set[State] = {
            State.DONE,
            State.CLOSED,
            State.ANSWERED,
            State.EPIC_CLOSED,
            State.EPIC_OPEN,
        }
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None:
                raise KeyError(ticket_id)
            if ticket.state in _NON_MARK_DONEABLE:
                raise TransitionError(
                    f"{ticket_id}: cannot mark done — "
                    f"state {ticket.state} is not eligible for mark-done"
                )
            comment = None
            if note.strip():
                comment = Comment(ticket_id=ticket_id, body=note, author=author)
                s.add(comment)
            event_note = f"mark done: {note}" if note else "mark done"
            ticket.state = State.DONE
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.flush()
            s.add(
                _make_event(s, ticket_id=ticket_id, state=State.DONE, note=event_note)
            )
            s.commit()
            if comment is not None:
                s.refresh(comment)
            s.refresh(ticket)
            if self._on_transition is not None:
                self._on_transition(ticket)
            return comment, ticket

    def get_proposed_action(self, action_id: int) -> ProposedAction | None:
        """Single-row lookup by primary key; returns ``None`` on miss."""
        with db.session(self.settings, self.board_id) as s:
            return s.get(ProposedAction, action_id)

    def approve_proposed_action(
        self, action_id: int, decided_by: str = "human"
    ) -> ProposedAction:
        """Approve a pending action and execute it.

        Transitions PENDING → APPROVED, stamps *decided_at* /
        *decided_by*, commits, then calls ``execute_proposed_action``
        (which sets EXECUTED or FAILED, captures ``failure_reason`` on
        failure, and writes audit/history notes for the mutation).

        Raises ``KeyError`` for an unknown *action_id* and
        ``ValueError`` if the action is not PENDING (including
        already-EXECUTED actions — safe to call, no double-execution).
        """
        with db.session(self.settings, self.board_id) as s:
            action = s.get(ProposedAction, action_id)
            if action is None:
                raise KeyError(action_id)
            if action.status != ProposedActionStatus.PENDING:
                raise ValueError(
                    f"ProposedAction {action_id}: cannot approve — "
                    f"status is {action.status.value}, not PENDING"
                )
            action.status = ProposedActionStatus.APPROVED
            action.decided_at = datetime.now(timezone.utc)
            action.decided_by = decided_by
            s.add(action)
            s.commit()
            s.refresh(action)

        self.execute_proposed_action(action_id, decided_by)

        with db.session(self.settings, self.board_id) as s:
            action = s.get(ProposedAction, action_id)
            s.refresh(action)
            return action

    def reject_proposed_action(
        self, action_id: int, decided_by: str = "human"
    ) -> ProposedAction:
        """Reject a pending action (no execution).

        Transitions PENDING → REJECTED.  Same error semantics as
        :meth:`approve_proposed_action` (``KeyError`` for unknown id,
        ``ValueError`` if not PENDING).
        """
        with db.session(self.settings, self.board_id) as s:
            action = s.get(ProposedAction, action_id)
            if action is None:
                raise KeyError(action_id)
            if action.status != ProposedActionStatus.PENDING:
                raise ValueError(
                    f"ProposedAction {action_id}: cannot reject — "
                    f"status is {action.status.value}, not PENDING"
                )
            action.status = ProposedActionStatus.REJECTED
            action.decided_at = datetime.now(timezone.utc)
            action.decided_by = decided_by
            s.add(action)
            s.commit()
            s.refresh(action)
            return action

    # --- proposed-action executor ----------------------------------------

    @staticmethod
    def _action_note(verb: str, source: str, rationale: str) -> str:
        """Format a ``TicketEvent`` note for a proposed action.

        Examples::

            "[health] closed via proposed action: stale ticket"
            "[trace-review] transitioned to ready via proposed action: …"
        """
        return f"[{source}] {verb} via proposed action: {rationale}"

    def execute_proposed_action(
        self, action_id: int, decided_by: str
    ) -> ProposedAction:
        """Execute an approved proposed action against its target ticket.

        Idempotent: calling on an already-EXECUTED or FAILED row returns
        it unchanged. Only APPROVED rows are dispatched.

        Raises :class:`KeyError` when *action_id* does not exist, and
        :class:`ValueError` when ``self.board_id`` is empty.
        """
        if not self.board_id:
            raise ValueError(
                "execute_proposed_action requires a board_id; "
                "call through a bound service instance"
            )

        # --- idempotency gate (load in a short-lived session) ---
        with db.session(self.settings, self.board_id) as s:
            action = s.get(ProposedAction, action_id)
            if action is None:
                raise KeyError(action_id)
            if action.status != ProposedActionStatus.APPROVED:
                return action
            # Snapshot fields before closing the session.
            action_type = action.action_type
            target_id = action.target_ticket_id
            payload = action.payload
            rationale = action.rationale
            source = action.source

        # --- dispatch ---
        failure: str | None = None
        try:
            if action_type == ActionType.CLOSE:
                self._execute_close(target_id, rationale, source)
            elif action_type == ActionType.TRANSITION:
                self._execute_transition(target_id, payload, rationale, source)
            elif action_type == ActionType.COMMENT:
                self._execute_comment(target_id, rationale, source)
            elif action_type == ActionType.RELABEL:
                self._execute_relabel(target_id, payload, rationale, source)
            else:
                raise ValueError(f"unknown action type: {action_type!r}")
        except (KeyError, TransitionError, ValueError, json.JSONDecodeError) as exc:
            failure = str(exc)
        except NotImplementedError as exc:
            failure = str(exc)

        # --- persist outcome ---
        status = (
            ProposedActionStatus.FAILED if failure else ProposedActionStatus.EXECUTED
        )
        with db.session(self.settings, self.board_id) as s:
            action = s.get(ProposedAction, action_id)
            # Double-check: the row may have been changed since our
            # first read (rare, but possible). If the status is no
            # longer APPROVED, bail out — someone else decided it.
            if action.status != ProposedActionStatus.APPROVED:
                return action
            action.status = status
            action.decided_at = datetime.now(timezone.utc)
            action.decided_by = decided_by
            action.failure_reason = failure
            s.add(action)
            s.commit()
            s.refresh(action)
            return action

    # -- dispatch helpers ------------------------------------------------

    def _execute_close(self, target_id: str, rationale: str, source: str) -> str:
        """Transition *target_id* to CLOSED with a proposed-action note."""
        self.transition(
            target_id,
            State.CLOSED,
            note=self._action_note("closed", source, rationale),
        )
        return "closed"

    def _execute_transition(
        self,
        target_id: str,
        payload: str | None,
        rationale: str,
        source: str,
    ) -> str:
        """Parse *payload* for a target state and transition *target_id*."""
        data = json.loads(payload or "{}")
        state_str = data["state"]
        dst = State(state_str)
        self.transition(
            target_id,
            dst,
            note=self._action_note(f"transitioned to {dst.value}", source, rationale),
        )
        return f"transitioned to {dst.value}"

    def _execute_comment(self, target_id: str, rationale: str, source: str) -> str:
        """Post *rationale* as a comment on *target_id* and leave a
        history breadcrumb."""
        self.add_comment(target_id, body=rationale, author=source)
        self.add_history_note(
            target_id,
            note=self._action_note("comment added", source, rationale),
        )
        return "comment added"

    def _execute_relabel(
        self,
        target_id: str,
        payload: str | None,
        rationale: str,
        source: str,
    ) -> str:
        """Apply a relabel *payload* to *target_id* and leave a history
        breadcrumb.

        Payload schema (JSON object):

        * ``set`` (optional ``list[str]``) — the ticket's labels become
          exactly this list.
        * otherwise ``add`` then ``remove`` (both optional ``list[str]``)
          are applied on top of the ticket's current labels.

        Raises :class:`ValueError` when none of ``set``/``add``/``remove``
        is present or any provided value is not a list of strings, and
        :class:`KeyError` when *target_id* is unknown.
        """
        data = json.loads(payload or "{}")

        def _as_str_list(value: object, key: str) -> list[str]:
            if not isinstance(value, list) or not all(
                isinstance(x, str) for x in value
            ):
                raise ValueError(f"relabel {key!r} must be a list of strings")
            return value

        new_labels: list[str]
        if "set" in data:
            new_labels = _as_str_list(data["set"], "set")
        elif "add" in data or "remove" in data:
            ticket = self.get(target_id)
            if ticket is None:
                raise KeyError(target_id)
            current = _parse_labels(ticket.labels)
            add = _as_str_list(data["add"], "add") if "add" in data else []
            remove = _as_str_list(data["remove"], "remove") if "remove" in data else []
            removed = set(remove)
            new_labels = [lbl for lbl in [*current, *add] if lbl not in removed]
        else:
            raise ValueError("relabel payload requires one of: set, add, remove")

        self.set_labels(target_id, new_labels)
        self.add_history_note(
            target_id,
            note=self._action_note("relabeled", source, rationale),
        )
        return f"relabeled: {new_labels}"
