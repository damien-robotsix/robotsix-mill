"""Verify runner — check TicketEvent hash-chain integrity."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from sqlmodel import select

from ..config import Settings
from ..core import db
from ..core.models import TicketEvent


@dataclass
class VerifyResult:
    total_events: int = 0
    tickets_verified: int = 0
    breaks: list[dict] = field(default_factory=list)


def _compute_hash(
    ticket_id: str,
    state: str,
    note: str | None,
    at: str,
    prev_hash: str | None,
) -> str:
    payload = {
        "ticket_id": ticket_id,
        "state": state,
        "note": note,
        "at": at,
        "prev_hash": prev_hash,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.blake2b(canonical.encode("utf-8"), digest_size=32).hexdigest()


def run_verify_pass(
    session_id: str,
    ticket_id: str | None = None,
) -> VerifyResult:
    """Walk TicketEvent hash chains and report integrity breaks.

    Skips events with an empty hash (pre-migration rows).
    """
    settings = Settings()
    boards: set[str] = set()

    # Determine which boards to scan. With the board-less default DB
    # gone, this is simply every registered repo's board_id plus any
    # board with a ``mill.db`` on disk (covers repos that were
    # registered transiently and have since been removed but still
    # carry historical TicketEvent rows worth verifying).
    try:
        from ..config import get_repos_config

        repos = get_repos_config().repos
        boards = {rc.board_id for rc in repos.values()}
    except Exception:
        pass
    try:
        for child in settings.data_dir.iterdir():
            if child.is_dir() and (child / "mill.db").exists():
                boards.add(child.name)
    except OSError:
        pass

    result = VerifyResult()
    seen_tickets: set[str] = set()

    for board_id in boards:
        with db.session(settings, board_id) as s:
            stmt = select(TicketEvent).order_by(TicketEvent.id)
            if ticket_id is not None:
                stmt = stmt.where(TicketEvent.ticket_id == ticket_id)
            events = list(s.exec(stmt).all())

            # Group events by ticket_id, preserving insertion order.
            chains: dict[str, list[TicketEvent]] = {}
            for ev in events:
                chains.setdefault(ev.ticket_id, []).append(ev)

            for tid, chain in chains.items():
                seen_tickets.add(tid)
                expected_prev: str | None = None
                chain_ok = True
                for ev in chain:
                    result.total_events += 1
                    if not ev.hash:
                        # Pre-migration event — skip verification.
                        expected_prev = ev.hash if ev.hash else None
                        continue
                    computed = _compute_hash(
                        ticket_id=ev.ticket_id,
                        state=ev.state.value,
                        note=ev.note,
                        at=ev.at.isoformat(),
                        prev_hash=ev.prev_hash,
                    )
                    if computed != ev.hash or ev.prev_hash != expected_prev:
                        result.breaks.append(
                            {
                                "event_id": ev.id,
                                "ticket_id": ev.ticket_id,
                                "field": (
                                    "hash" if computed != ev.hash else "prev_hash"
                                ),
                            }
                        )
                        chain_ok = False
                    expected_prev = ev.hash
                if chain_ok:
                    result.tickets_verified += 1

    return result
