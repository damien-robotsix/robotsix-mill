"""State-machine and metadata mutation surface of :class:`TicketService`
(``_LifecycleMixin``)."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from secrets import token_hex
from typing import Any, cast

from sqlmodel import Session, col, select

from .. import db
from ..models import (
    Comment,
    SourceKind,
    Ticket,
    TicketEvent,
    TicketKind,
)
from ..states import ASK_USER_MARKER, State, can_transition
from ..workspace import Workspace, prune_clone
from ._base import _ServiceBase
from ._helpers import (
    TransitionError,
    _get_ticket,
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


# --- PR/commit citation verification for mark_done -----------------------

# Matches "#NNNNN" or "PR #NNNNN" — PR number references in free-text notes.
_PR_CITATION_RE = re.compile(r"(?:PR\s+)?#(\d{1,5})", re.IGNORECASE)

# Matches 7–40 hex SHA-like tokens (same pattern as refine's _COMMIT_SHA_RE).
_COMMIT_CITATION_RE = re.compile(r"\b[0-9a-f]{7,40}\b")


def _verify_citations(note: str, repo_dir: Path | None) -> str:
    """Best-effort: check cited PRs / commit SHAs against *repo_dir*'s
    ``origin/main`` and append ⚠️ warnings for any that can't be verified.

    Returns *note* unchanged when *repo_dir* is ``None`` or missing,
    when *note* is empty, or when no citations are detected.
    """
    if not repo_dir or not repo_dir.exists():
        return note
    if not note or not note.strip():
        return note

    warnings: list[str] = []

    # --- PR citations: git log --grep="#N" origin/main ------------------
    for m in _PR_CITATION_RE.finditer(note):
        pr_num = m.group(1)
        grep = f"#{pr_num}"
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_dir),
                    "log",
                    "--oneline",
                    f"--grep={grep}",
                    "origin/main",
                    "-1",
                ],
                capture_output=True,
                text=True,
            )
        except Exception:
            # If git itself is broken, skip verification entirely.
            return note
        if result.returncode != 0 or not result.stdout.strip():
            warnings.append(f"PR #{pr_num}")

    # --- Commit SHA citations: git cat-file -e + merge-base ------------
    for m in _COMMIT_CITATION_RE.finditer(note):
        sha = m.group(0)
        # Skip SHAs that are embedded inside PR references already handled above.
        try:
            type_check = subprocess.run(
                ["git", "-C", str(repo_dir), "cat-file", "-e", sha],
                capture_output=True,
                text=True,
            )
        except Exception:
            return note
        if type_check.returncode != 0:
            warnings.append(f"commit {sha}")
            continue
        try:
            anc = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_dir),
                    "merge-base",
                    "--is-ancestor",
                    sha,
                    "origin/main",
                ],
                capture_output=True,
                text=True,
            )
        except Exception:
            return note
        if anc.returncode != 0:
            warnings.append(f"commit {sha}")

    if not warnings:
        return note

    lines: list[str] = []
    for w in sorted(set(warnings)):
        lines.append(
            f"⚠️ {w} not found on origin/main at time of closure — verify manually."
        )
    return note.rstrip() + "\n\n" + "\n".join(lines)


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
        kind: TicketKind = TicketKind.TASK,
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

        if kind in (TicketKind.INQUIRY, TicketKind.EPIC) and depends_on:
            raise ValueError(f"{kind}s do not support depends_on — they are standalone")

        # Reject self-dependency before persisting.
        if depends_on:
            dep_ids = _parse_depends_on_str(depends_on)
            if ticket_id in dep_ids:
                raise ValueError(f"Ticket cannot depend on itself: {ticket_id}")

        if kind == TicketKind.EPIC:
            initial_state = State.EPIC_OPEN
        elif kind == TicketKind.INQUIRY:
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

        # Validate parent_id against ANY board (cross-board parent links are
        # supported — the epic may live on a different board than its child).
        if parent_id is not None:
            parent = self.get(parent_id)
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
            ticket = _get_ticket(s, ticket_id)
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
            Comment.body.startswith(ASK_USER_MARKER),
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
            ticket = _get_ticket(s, ticket_id)
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
            ticket = _get_ticket(s, ticket_id)
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
            ticket = _get_ticket(s, ticket_id)
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
            ticket = _get_ticket(s, ticket_id)
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
            ticket = _get_ticket(s, ticket_id)
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
            # no-op baseline. Resolve the ticket's ``repo_config`` (by
            # board_id) so the read qualifies the session id the same way
            # the tracer stamped it (``<repo> · <id>``); without it the
            # baseline would query the bare id, read $0, and fail to reset
            # the dollar-cap on redraft.
            from ...config import get_repos_config
            from ...langfuse.client import session_cost

            repo_config = next(
                (
                    rc
                    for rc in get_repos_config().repos.values()
                    if rc.board_id == ticket.board_id
                ),
                None,
            )
            ticket.pre_redraft_cost_usd = session_cost(
                self.settings, ticket_id, repo_config=repo_config, force=True
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
            ticket = _get_ticket(s, ticket_id)
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

        Before persisting, cited PR numbers and commit SHAs in *note*
        are verified against ``origin/main`` in the ticket's workspace
        clone; unverifiable citations get a ⚠️ warning appended (soft
        warning — the closure still proceeds).

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
        try:
            board = self._board_for(ticket_id)
        except ValueError:
            board = self.board_id or ""
        with db.session(self.settings, board) as s:
            ticket = _get_ticket(s, ticket_id)
            if ticket.state in _NON_MARK_DONEABLE:
                raise TransitionError(
                    f"{ticket_id}: cannot mark done — "
                    f"state {ticket.state} is not eligible for mark-done"
                )
            # Augment the note with citation warnings before persisting.
            repo_dir = self.workspace(ticket).repo_dir
            note = _verify_citations(note, repo_dir)
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

    _MIGRATABLE_EPIC_STATES: set[State] = _MIGRATABLE_STATES | {
        State.EPIC_OPEN,
        State.EPIC_CLOSED,
    }

    @staticmethod
    def _collect_subtree(session: Session, root_id: str) -> list[Ticket]:
        """Collect the full descendant subtree rooted at *root_id*.

        Returns tickets in insertion order (root first, then BFS
        children, grandchildren, ...).  Uses an iterative scan — no
        recursion — so deep trees cannot overflow the Python stack.
        """
        root = session.get(Ticket, root_id)
        if root is None:
            raise KeyError(root_id)
        subtree: list[Ticket] = [root]
        i = 0
        while i < len(subtree):
            children = list(
                session.exec(
                    select(Ticket).where(Ticket.parent_id == subtree[i].id)
                ).all()
            )
            subtree.extend(children)
            i += 1
        return subtree

    def _migrate_epic_subtree(
        self,
        s: Session,
        root: Ticket,
        src_board: str,
        dst_board: str,
        note: str | None = None,
    ) -> Ticket:
        """Migrate an epic and its entire descendant subtree atomically.

        *s* is the already-open source-DB session (read-only — we only
        snapshot from it).  All mutations happen in fresh sessions.
        """
        # 1. Collect the full subtree (root first, then BFS).
        subtree = self._collect_subtree(s, root.id)

        # 2. Validate states for every ticket in the subtree.
        blockers: list[str] = []
        for t in subtree:
            state = State(t.state)
            if t.kind == TicketKind.EPIC:
                if state not in self._MIGRATABLE_EPIC_STATES:
                    blockers.append(f"  {t.id}: epic in state {state.value!r}")
            else:
                if state not in self._MIGRATABLE_STATES:
                    blockers.append(f"  {t.id}: in state {state.value!r}")
        if blockers:
            allowed = ", ".join(sorted(st.value for st in self._MIGRATABLE_STATES))
            epic_allowed = ", ".join(
                sorted(st.value for st in self._MIGRATABLE_EPIC_STATES)
            )
            raise ValueError(
                f"migrate: cannot migrate epic subtree — the following "
                f"tickets are in non-migratable states (allowed: "
                f"[{allowed}]; epic allowed: [{epic_allowed}]):\n" + "\n".join(blockers)
            )

        # 3. Snapshot every ticket's data from the source DB.
        snapshots: dict[str, Any] = {}
        for t in subtree:
            snapshots[t.id] = {
                "ticket": t.model_dump(),
                "events": [
                    ev.model_dump()
                    for ev in s.exec(
                        select(TicketEvent)
                        .where(TicketEvent.ticket_id == t.id)
                        .order_by(col(TicketEvent.id))
                    ).all()
                ],
                "comments": [
                    c.model_dump()
                    for c in s.exec(
                        select(Comment)
                        .where(Comment.ticket_id == t.id)
                        .order_by(col(Comment.id))
                    ).all()
                ],
            }

        # 4. Move workspace dirs (fail early, before any DB write).
        dst_root = self.settings.workspaces_dir_for(dst_board)
        dst_root.mkdir(parents=True, exist_ok=True)
        ws_moves: list[tuple[Path, Path, bool]] = []  # (src, dst, moved)
        for t in subtree:
            src_ws = self.settings.workspaces_dir_for(src_board) / t.id
            dst_ws = dst_root / t.id
            if dst_ws.exists():
                raise ValueError(f"migrate: workspace already exists at {dst_ws}")
            moved = src_ws.exists()
            if moved:
                shutil.move(str(src_ws), str(dst_ws))
            else:
                dst_ws.mkdir(parents=True, exist_ok=True)
            # Drop repo-specific leftovers: clones target the OLD repo
            # and a cached baseline verdict would replay against the
            # wrong tree.
            shutil.rmtree(dst_ws / "repo", ignore_errors=True)
            shutil.rmtree(dst_ws / "repos", ignore_errors=True)
            (dst_ws / "artifacts" / "baseline_check.json").unlink(missing_ok=True)
            ws_moves.append((src_ws, dst_ws, moved))

        # 5. Insert every ticket into the target DB (parent-before-child).
        global_id_map: dict[int, int] = {}
        try:
            with db.session(self.settings, dst_board) as s2:
                for t in subtree:
                    snap = snapshots[t.id]
                    td = snap["ticket"]
                    dst_ws = dst_root / t.id
                    migration_note = (
                        f"migrated from board {src_board!r} to {dst_board!r}"
                    )
                    old_state = State(td["state"])
                    if old_state is not State.DRAFT:
                        migration_note += f" (was {old_state.value})"
                    if note and t.id == root.id:
                        migration_note += f": {note}"

                    td.update(
                        state=State.DRAFT,
                        board_id=dst_board,
                        workspace_path=str(dst_ws),
                        branch=None,
                        blocked_from=None,
                        paused_from=None,
                        review_rounds=0,
                        implement_cycles=0,
                        retry_attempt=0,
                        last_transient_error=None,
                        next_retry_at=None,
                        updated_at=datetime.now(timezone.utc),
                    )
                    s2.add(Ticket(**td))

                    for ev in snap["events"]:
                        ev["id"] = None
                        s2.add(TicketEvent(**ev))

                    for cd in snap["comments"]:
                        old_id = cd["id"]
                        cd["id"] = None
                        if cd.get("parent_id") is not None:
                            cd["parent_id"] = global_id_map.get(cd["parent_id"])
                        comment = Comment(**cd)
                        s2.add(comment)
                        s2.flush()
                        if comment.id is None:  # pragma: no cover
                            raise RuntimeError(
                                "migrate: comment id missing after flush"
                            )
                        global_id_map[old_id] = comment.id

                    s2.flush()
                    s2.add(
                        _make_event(
                            s2,
                            ticket_id=t.id,
                            state=State.DRAFT,
                            note=migration_note,
                        )
                    )
                s2.commit()
        except Exception:
            # Roll workspace dirs back to the source board.
            for src_ws, dst_ws, moved in ws_moves:
                if moved:
                    if dst_ws.exists():
                        shutil.move(str(dst_ws), str(src_ws))
                else:
                    shutil.rmtree(dst_ws, ignore_errors=True)
            raise

        # 6. Delete every ticket from the source DB (reverse order).
        with db.session(self.settings, src_board) as s3:
            for t in reversed(subtree):
                for comment in s3.exec(
                    select(Comment).where(Comment.ticket_id == t.id)
                ).all():
                    s3.delete(comment)
                for ev in s3.exec(
                    select(TicketEvent).where(TicketEvent.ticket_id == t.id)
                ).all():
                    s3.delete(ev)
                src_ticket = s3.get(Ticket, t.id)
                if src_ticket is not None:
                    s3.delete(src_ticket)
            s3.commit()

        log.info(
            "migrate: epic subtree %s (%d tickets) %s -> %s",
            root.id,
            len(subtree),
            src_board,
            dst_board,
        )
        migrated = self.get(root.id)
        if migrated is None:  # pragma: no cover - defensive
            raise RuntimeError(f"migrate: {root.id} vanished during migration")
        return migrated

    def migrate(
        self, ticket_id: str, target_board: str, note: str | None = None
    ) -> Ticket:
        """Move a ticket to another board: its row, history events,
        comments, and workspace directory.

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
            ticket = _get_ticket(s, ticket_id)
            if ticket.kind == TicketKind.EPIC:
                return self._migrate_epic_subtree(s, ticket, src_board, dst_board, note)
            # A non-epic ticket's parent stays on the source board.  Cross-board
            # parent links are now supported — the link survives the move intact.
            unlinked_parent = ticket.parent_id or None
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
                    parent_id=unlinked_parent,  # cross-board parent link survives
                    branch=None,
                    blocked_from=None,
                    paused_from=None,
                    review_rounds=0,
                    implement_cycles=0,
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

    def _maybe_purge_ticket_events(self, ticket_id: str) -> int:
        """Prune oldest TicketEvent rows for *ticket_id* when the count
        exceeds ``max_events_per_ticket``, keeping only the most recent.

        After deletion, sets ``prev_hash = None`` on the new earliest
        remaining event so the hash chain starts cleanly at the prune
        point.  Returns the number of rows deleted (0 when under cap
        or when the cap is disabled).
        """
        max_events = self.settings.max_events_per_ticket
        if max_events <= 0:
            return 0

        with db.session(self.settings, self.board_id) as s:
            all_events = s.exec(
                select(TicketEvent)
                .where(TicketEvent.ticket_id == ticket_id)
                .order_by(col(TicketEvent.id))
            ).all()

            total = len(all_events)
            if total <= max_events:
                return 0

            excess = total - max_events
            # Delete the oldest *excess* events.
            for ev in all_events[:excess]:
                s.delete(ev)

            # Reset prev_hash on the new earliest remaining event.
            earliest = all_events[excess] if excess < len(all_events) else None
            if earliest is not None and earliest.prev_hash is not None:
                earliest.prev_hash = None
                s.add(earliest)

            s.commit()
            return excess

    def _maybe_purge_ticket_comments(self, ticket_id: str) -> int:
        """Prune oldest unprotected Comment rows for *ticket_id* when the
        count exceeds ``max_comments_per_ticket``, keeping only the most
        recent.

        OPEN threads (top-level comments with ``closed_at IS NULL``) and
        their replies are **protected** — never deleted — so
        ``[ASK_USER]`` auto-resume and active discussions are preserved
        even when the ticket exceeds the cap.

        After deletions, any surviving reply whose ``parent_id``
        references a deleted comment has its ``parent_id`` reset to
        ``None``, mirroring the ``prev_hash`` reset in
        ``_maybe_purge_ticket_events``.

        Returns the number of rows deleted (0 when under cap, when the
        cap is disabled, or when there are no unprotected comments).
        """
        max_comments = self.settings.max_comments_per_ticket
        if max_comments <= 0:
            return 0

        with db.session(self.settings, self.board_id) as s:
            all_comments = s.exec(
                select(Comment)
                .where(Comment.ticket_id == ticket_id)
                .order_by(col(Comment.id))
            ).all()

            total = len(all_comments)
            if total <= max_comments:
                return 0

            # --- protected set: open threads and their replies ---
            # An "open thread" is a top-level comment (parent_id IS NULL)
            # whose closed_at IS NULL.  Every reply (parent_id IS NOT NULL)
            # whose top-level ancestor is open is also protected.
            open_root_ids: set[int] = set()
            for c in all_comments:
                cid = cast(int, c.id)  # DB-loaded comment, id is never None
                if c.parent_id is None and c.closed_at is None:
                    open_root_ids.add(cid)

            protected_ids: set[int] = set()
            for c in all_comments:
                cid = cast(int, c.id)  # DB-loaded comment, id is never None
                if cid in open_root_ids:
                    protected_ids.add(cid)
                    continue
                if c.parent_id is not None:
                    # Walk up to find the root ancestor.
                    ancestor_pid: int | None = c.parent_id
                    # Guard against cycles (should never exist).
                    seen: set[int] = {cid}
                    while ancestor_pid is not None:
                        if ancestor_pid in seen:
                            break
                        if ancestor_pid in open_root_ids:
                            protected_ids.add(cid)
                            break
                        seen.add(ancestor_pid)
                        # Find the parent comment in the loaded list.
                        parent = next(
                            (x for x in all_comments if x.id == ancestor_pid), None
                        )
                        ancestor_pid = parent.parent_id if parent else None

            # --- delete oldest unprotected excess ---
            unprotected = [c for c in all_comments if c.id not in protected_ids]
            excess = total - max_comments
            deleted_ids: set[int] = set()
            deleted = 0
            for c in unprotected:
                if deleted >= excess:
                    break
                cid = cast(int, c.id)  # DB-loaded comment, id is never None
                s.delete(c)
                deleted_ids.add(cid)
                deleted += 1

            # --- reset parent_id on surviving replies that referenced
            # a now-deleted comment ---
            if deleted_ids:
                for c in all_comments:
                    if c.id not in deleted_ids and c.parent_id in deleted_ids:
                        c.parent_id = None
                        s.add(c)

            s.commit()
            return deleted

    def db_maintenance_pass(self) -> dict[str, int]:
        """Run one DB maintenance sweep: archive purge, per-ticket event
        cap, and SQLite ``PRAGMA optimize``.

        Returns a summary dict with keys ``archived_purged``,
        ``events_pruned``, ``comments_pruned``, and ``tickets_pruned``.
        """
        result: dict[str, int] = {
            "archived_purged": 0,
            "events_pruned": 0,
            "comments_pruned": 0,
            "tickets_pruned": 0,
        }

        # 1. Count terminal tickets before purge, then run it.
        with db.session(self.settings, self.board_id) as s:
            before = s.exec(
                select(Ticket).where(
                    col(Ticket.state).in_(list(self._ARCHIVABLE_STATES))
                )
            ).all()
        before_count = len(before)
        self._maybe_purge_archived()
        with db.session(self.settings, self.board_id) as s:
            after = s.exec(
                select(Ticket).where(
                    col(Ticket.state).in_(list(self._ARCHIVABLE_STATES))
                )
            ).all()
        result["archived_purged"] = before_count - len(after)

        # 2. Event cap for ALL non-terminal tickets.
        with db.session(self.settings, self.board_id) as s:
            active_ids = s.exec(
                select(Ticket.id).where(
                    col(Ticket.state).notin_(list(self._ARCHIVABLE_STATES))
                )
            ).all()
        for tid in active_ids:
            pruned = self._maybe_purge_ticket_events(tid)
            if pruned:
                result["events_pruned"] += pruned
                result["tickets_pruned"] += 1
            pruned_c = self._maybe_purge_ticket_comments(tid)
            if pruned_c:
                result["comments_pruned"] += pruned_c

        # 3. Reclaim freed pages and truncate the WAL file.
        with db.session(self.settings, self.board_id) as s:
            s.connection().exec_driver_sql("PRAGMA optimize")
            s.connection().exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
            s.commit()

        return result
