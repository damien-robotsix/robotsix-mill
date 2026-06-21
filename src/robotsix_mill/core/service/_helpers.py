"""Module-level helpers and the ``TransitionError`` exception.

Hash-chain event helpers, slug / JSON-list parsing utilities, and the
state-machine :class:`TransitionError`, factored out of ``service.py`` so
the mixin modules (and the package ``__init__``) can share them without a
circular import.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

from sqlmodel import select

from ..models import Ticket, TicketEvent
from ..states import State


def _get_ticket(db_session, ticket_id: str) -> Ticket:
    """Return the Ticket for *ticket_id*, or raise ``KeyError``."""
    ticket = db_session.get(Ticket, ticket_id)
    if ticket is None:
        raise KeyError(ticket_id)
    return ticket


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


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-")[:40].strip("-") or "ticket"


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
