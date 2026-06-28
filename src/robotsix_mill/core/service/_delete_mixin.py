"""Ticket-deletion and redraft surface of :class:`TicketService` (``_DeleteMixin``)."""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone

from sqlmodel import select

from .. import db
from ..models import (
    Comment,
    Ticket,
    TicketEvent,
)
from ..states import State
from ..workspace import prune_clone
from ._base import _ServiceBase
from ._helpers import (
    TransitionError,
    _get_ticket,
    _make_event,
)

log = logging.getLogger("robotsix_mill.service")


class _DeleteMixin(_ServiceBase):
    """Hard-delete and redraft (re-create after delete)."""

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
