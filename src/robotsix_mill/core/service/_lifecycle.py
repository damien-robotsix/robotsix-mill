"""State-machine and metadata mutation surface of :class:`TicketService`
(``_LifecycleMixin``)."""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from secrets import token_hex

from sqlmodel import Session, col, select

from .. import db
from ..models import (
    Comment,
    ProposedAction,
    ProposedActionStatus,
    SourceKind,
    Ticket,
    TicketEvent,
)
from ..states import State, can_transition
from ..workspace import Workspace, prune_clone
from ._base import _ServiceBase
from ._helpers import (
    TransitionError,
    _make_event,
    _parse_depends_on_str,
    _slug,
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

# States that represent a terminal pipeline outcome — transitions to
# these are gated on having no open [ASK_USER] threads.
_TERMINAL_STATES: set[State] = {
    State.DONE,
    State.CLOSED,
    State.ERRORED,
}


class _LifecycleMixin(_ServiceBase):
    """Ticket creation, state transitions, and metadata mutation."""

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
            from ...config import get_repos_config

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

    def _has_open_ask_user_threads(
        self, ticket_id: str, session: Session
    ) -> list[Comment]:
        """Return open top-level ``[ASK_USER]`` comment threads on
        *ticket_id* (those with ``closed_at IS NULL``)."""
        stmt = select(Comment).where(
            Comment.ticket_id == ticket_id,
            Comment.parent_id == None,  # noqa: E711 (SQLAlchemy IS NULL)
            Comment.body.startswith("[ASK_USER]"),
            Comment.closed_at == None,  # noqa: E711
        )
        return list(session.exec(stmt).all())

    def close_open_ask_user_threads(self, ticket_id: str) -> int:
        """Close every open ``[ASK_USER]`` thread on *ticket_id*; return the
        count closed.

        Used when the pipeline AUTO-completes a ticket (e.g. a merged PR
        reaching DONE) whose open questions are now moot — the work shipped,
        so a stale thread must not block the terminal transition (which would
        otherwise raise ``TransitionError`` and crash the worker consumer in a
        loop). The thread is closed-with-record (not deleted), so the question
        text is preserved in history.
        """
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            open_threads = self._has_open_ask_user_threads(ticket_id, s)
            now = datetime.now(timezone.utc)
            for c in open_threads:
                c.closed_at = now
                s.add(c)
            s.commit()
            return len(open_threads)

    def transition(self, ticket_id: str, dst: State, note: str | None = None) -> Ticket:
        """Move a ticket to *dst* state.

        Returns the updated :class:`Ticket`. Raises :class:`KeyError` if
        the ticket does not exist and :class:`TransitionError` if the
        transition is not allowed by the state machine.

        When transitioning to :class:`State.BLOCKED`, the originating
        state is recorded in ``blocked_from`` so it can be resumed later.

        Transitions to terminal states — :class:`State.DONE`,
        :class:`State.CLOSED`, or :class:`State.ERRORED` — are rejected
        when the ticket has any open ``[ASK_USER]`` comment threads.
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
            # Refuse to transition to a terminal state while any
            # [ASK_USER] threads remain open — those questions must be
            # resolved (thread closed) before the pipeline completes.
            if dst in _TERMINAL_STATES:
                open_threads = self._has_open_ask_user_threads(ticket_id, s)
                if open_threads:
                    ids = ", ".join(str(t.id) for t in open_threads)
                    raise TransitionError(
                        f"{ticket_id}: cannot transition to {dst} while "
                        f"{len(open_threads)} [ASK_USER] thread(s) are "
                        f"open (IDs: {ids})"
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
            # Auto-reject stale PENDING proposals when the ticket enters a
            # terminal (archivable) state — there is nothing left to approve.
            if dst in self._ARCHIVABLE_STATES:
                _now = datetime.now(timezone.utc)
                pending = s.exec(
                    select(ProposedAction).where(
                        ProposedAction.target_ticket_id == ticket_id,
                        ProposedAction.status == ProposedActionStatus.PENDING,
                    )
                ).all()
                for pa in pending:
                    pa.status = ProposedActionStatus.REJECTED
                    pa.decided_at = _now
                    pa.decided_by = "system"
                    s.add(pa)
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
            from ...langfuse.client import session_cost

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
            for pa in s.exec(
                select(ProposedAction).where(
                    ProposedAction.target_ticket_id == ticket_id
                )
            ).all():
                s.delete(pa)
            for c in s.exec(
                select(Comment).where(Comment.ticket_id == ticket_id)
            ).all():
                s.delete(c)
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

    # States from which a cross-board migration is safe: no stage is
    # actively producing repo-bound artifacts and no PR is in flight.
    _MIGRATABLE_STATES: set[State] = {
        State.DRAFT,
        State.READY,
        State.BLOCKED,
        State.ERRORED,
        State.MAINTENANCE,
    }

    def migrate(
        self, ticket_id: str, target_board: str, note: str | None = None
    ) -> Ticket:
        """Move a ticket to another board: its row, history events,
        comments, proposed actions, and workspace directory.

        The migrated ticket lands in ``DRAFT`` on the target board so
        its refine stage re-triages it with the right repo context.
        Repo-specific baggage is reset: ``branch``, retry state,
        ``review_rounds``, ``blocked_from``/``paused_from``, the
        ``repo/``/``repos/`` clones, and the cached
        ``baseline_check.json`` (stale verdicts from the old repo must
        not replay on the new one). The history hash chain is preserved
        verbatim and extended with a migration event.

        *target_board* accepts a board id or a repo id (``"meta"``
        included). Raises :class:`KeyError` when the ticket does not
        exist and :class:`ValueError` for an unknown target, a same-board
        move, an epic / parent-linked ticket, or a state outside
        ``_MIGRATABLE_STATES``.
        """
        from ...config import get_repos_config

        if not target_board:
            raise ValueError("migrate: target board is required")

        # Resolve repo-id → board-id and validate against the registry.
        # "meta" is the synthetic cross-repo board (not in repos).
        known: dict[str, str] = {"meta": "meta"}
        try:
            for rid, rc in get_repos_config().repos.items():
                known[rid] = rc.board_id
                known[rc.board_id] = rc.board_id
        except Exception:
            pass
        dst_board = known.get(target_board)
        if dst_board is None:
            raise ValueError(
                f"migrate: unknown target board {target_board!r}. "
                f"Known boards: {sorted(set(known.values()))}"
            )

        src_board = self._board_for(ticket_id)
        if dst_board == src_board:
            raise ValueError(f"migrate: {ticket_id} is already on board {src_board!r}")

        # --- snapshot everything from the source DB (no mutation yet) ---
        with db.session(self.settings, src_board) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None:
                raise KeyError(ticket_id)
            if ticket.kind == "epic":
                raise ValueError("migrate: epics cannot be migrated")
            if ticket.parent_id:
                raise ValueError(
                    f"migrate: {ticket_id} is linked to parent "
                    f"{ticket.parent_id!r} on board {src_board!r} — unlink first"
                )
            if (
                s.exec(
                    select(Ticket).where(Ticket.parent_id == ticket_id).limit(1)
                ).first()
                is not None
            ):
                raise ValueError(
                    f"migrate: {ticket_id} has child tickets — migrate or unlink them first"
                )
            state = State(ticket.state)
            if state not in self._MIGRATABLE_STATES:
                allowed = ", ".join(sorted(st.value for st in self._MIGRATABLE_STATES))
                raise ValueError(
                    f"migrate: {ticket_id} is {state.value!r} — only "
                    f"[{allowed}] tickets can be migrated"
                )
            ticket_data = ticket.model_dump()
            event_data = [
                ev.model_dump()
                for ev in s.exec(
                    select(TicketEvent)
                    .where(TicketEvent.ticket_id == ticket_id)
                    .order_by(col(TicketEvent.id))
                ).all()
            ]
            comment_data = [
                c.model_dump()
                for c in s.exec(
                    select(Comment)
                    .where(Comment.ticket_id == ticket_id)
                    .order_by(col(Comment.id))
                ).all()
            ]
            action_data = [
                a.model_dump()
                for a in s.exec(
                    select(ProposedAction)
                    .where(ProposedAction.target_ticket_id == ticket_id)
                    .order_by(col(ProposedAction.id))
                ).all()
            ]

        # --- move the workspace directory (fail early, before any DB write) ---
        src_ws = self.settings.workspaces_dir_for(src_board) / ticket_id
        dst_root = self.settings.workspaces_dir_for(dst_board)
        dst_ws = dst_root / ticket_id
        if dst_ws.exists():
            raise ValueError(f"migrate: workspace already exists at {dst_ws}")
        dst_root.mkdir(parents=True, exist_ok=True)
        ws_moved = src_ws.exists()
        if ws_moved:
            shutil.move(str(src_ws), str(dst_ws))
        else:
            dst_ws.mkdir(parents=True, exist_ok=True)
        # Drop repo-specific leftovers: clones target the OLD repo and a
        # cached baseline verdict would replay against the wrong tree.
        shutil.rmtree(dst_ws / "repo", ignore_errors=True)
        shutil.rmtree(dst_ws / "repos", ignore_errors=True)
        (dst_ws / "artifacts" / "baseline_check.json").unlink(missing_ok=True)

        migration_note = f"migrated from board {src_board!r} to {dst_board!r}"
        if state is not State.DRAFT:
            migration_note += f" (was {state.value})"
        if note:
            migration_note += f": {note}"

        # --- insert into the target DB ---
        try:
            with db.session(self.settings, dst_board) as s:
                ticket_data.update(
                    state=State.DRAFT,
                    board_id=dst_board,
                    workspace_path=str(dst_ws),
                    branch=None,
                    blocked_from=None,
                    paused_from=None,
                    review_rounds=0,
                    retry_attempt=0,
                    last_transient_error=None,
                    next_retry_at=None,
                    updated_at=datetime.now(timezone.utc),
                )
                s.add(Ticket(**ticket_data))
                for ev in event_data:
                    ev["id"] = None  # fresh autoincrement in the target DB
                    s.add(TicketEvent(**ev))
                # Comments self-reference via parent_id — remap as we go
                # (a parent's id always precedes its replies').
                id_map: dict[int, int] = {}
                for cd in comment_data:
                    old_id = cd["id"]
                    cd["id"] = None
                    if cd.get("parent_id") is not None:
                        cd["parent_id"] = id_map.get(cd["parent_id"])
                    comment = Comment(**cd)
                    s.add(comment)
                    s.flush()
                    if comment.id is None:  # pragma: no cover - flush assigns the pk
                        raise RuntimeError("migrate: comment id missing after flush")
                    id_map[old_id] = comment.id
                for ad in action_data:
                    ad["id"] = None
                    s.add(ProposedAction(**ad))
                s.flush()
                s.add(
                    _make_event(
                        s,
                        ticket_id=ticket_id,
                        state=State.DRAFT,
                        note=migration_note,
                    )
                )
                s.commit()
        except Exception:
            # Roll the workspace back so the source board stays intact.
            if ws_moved:
                shutil.move(str(dst_ws), str(src_ws))
            else:
                shutil.rmtree(dst_ws, ignore_errors=True)
            raise

        # --- remove from the source DB (the target copy is committed) ---
        with db.session(self.settings, src_board) as s:
            for action in s.exec(
                select(ProposedAction).where(
                    ProposedAction.target_ticket_id == ticket_id
                )
            ).all():
                s.delete(action)
            for comment in s.exec(
                select(Comment).where(Comment.ticket_id == ticket_id)
            ).all():
                s.delete(comment)
            for src_ev in s.exec(
                select(TicketEvent).where(TicketEvent.ticket_id == ticket_id)
            ).all():
                s.delete(src_ev)
            src_ticket = s.get(Ticket, ticket_id)
            if src_ticket is not None:
                s.delete(src_ticket)
            s.commit()

        log.info("migrate: %s %s -> %s", ticket_id, src_board, dst_board)
        migrated = self.get(ticket_id)
        if migrated is None:  # pragma: no cover - defensive
            raise RuntimeError(f"migrate: {ticket_id} vanished during migration")
        return migrated

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

    # _maybe_purge_stale_proposed_actions moved to _ActionMixin.
