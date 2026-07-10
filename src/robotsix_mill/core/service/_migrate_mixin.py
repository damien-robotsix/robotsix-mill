"""Ticket-migration surface of :class:`TicketService` (``_MigrateMixin``)."""

from __future__ import annotations

import logging
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlmodel import Session, col, select

from sqlalchemy import exc as sa_exc

from .. import db
from ..models import (
    Comment,
    Ticket,
    TicketEvent,
    TicketKind,
)
from ..states import State
from ._base import _ServiceBase
from ._helpers import (
    _get_ticket,
    _make_event,
)

log = logging.getLogger("robotsix_mill.service")


class _MigrateMixin(_ServiceBase):
    """Cross-board ticket and epic-subtree migration."""

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
        except (
            OSError,
            RuntimeError,
            ValueError,
            sa_exc.SQLAlchemyError,
            sqlite3.OperationalError,
        ):
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
            # get_repos_config() reads the repo registry YAML; if it is
            # unavailable or malformed we continue with only the "meta"
            # board known — migrate will catch unknown targets later.
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
        except (
            OSError,
            RuntimeError,
            ValueError,
            sa_exc.SQLAlchemyError,
            sqlite3.OperationalError,
        ):
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

        # ticket_id has been validated by DB lookups above; guard log
        # output against accidental newline injection anyway.
        _safe_tid = ticket_id.replace("\n", "\\n").replace("\r", "\\r")
        log.info("migrate: %s %s -> %s", _safe_tid, src_board, dst_board)
        migrated = self.get(ticket_id)
        if migrated is None:  # pragma: no cover - defensive
            raise RuntimeError(f"migrate: {ticket_id} vanished during migration")
        return migrated
