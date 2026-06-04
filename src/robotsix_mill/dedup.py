"""Shared ticket-dedup primitives.

Source-agnostic helpers for spotting that a would-be new ticket
duplicates one that was recently filed (or already shipped). Extracted
from ``trace_review_runner`` so multiple producers (trace-review,
epic-decomposition pre-filing checks, …) share one matching seam
instead of each growing its own copy.

The matcher is best-effort: any query failure logs and returns
``None`` rather than raising into the caller.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection, Sequence
from datetime import datetime, timedelta, timezone

from .config import Settings
from .core.models import SourceKind, Ticket
from .core.service import TicketService
from .core.states import State
from .core.workspace import Workspace

log = logging.getLogger("robotsix_mill.dedup")


def normalize(s: str) -> str:
    """Lower-case *s* and collapse every run of non-alphanumeric
    characters into a single space, stripping the ends."""
    return re.sub(r"[^a-z0-9]+", " ", s.casefold()).strip()


def find_prior_matching_ticket(
    service: TicketService,
    board_id: str,
    target_files: list[str],
    fingerprint_text: str,
    settings: Settings,
    now: datetime,
    *,
    sources: Sequence[SourceKind] | None = None,
    lookback_days: int = 7,
    exclude_ids: Collection[str] = (),
) -> Ticket | None:
    """Look up recent tickets on *board_id* and return the first one
    that matches the given fix signal.

    A candidate matches when, within the recency window
    (``now - timedelta(days=lookback_days)``):
    - any path in *target_files* appears verbatim in the candidate's
      description body, OR
    - the normalized fingerprint (first ~60 normalized chars of
      *fingerprint_text*) appears in the candidate's normalized title.

    *sources* restricts the candidate pool: ``None`` matches across
    every source, a sequence unions the listed kinds. *exclude_ids*
    skips candidates by ``id`` (e.g. the epic itself and its existing
    children).

    Candidates in ERRORED state, and CLOSED candidates that were never
    DONE (declined drafts), are EXCLUDED — neither is a fix, so a new
    occurrence deserves a fresh draft.

    Returns ``None`` when no match is found.
    """
    try:
        cutoff = now - timedelta(days=lookback_days)
        candidates = service.recent_tickets(limit=200, sources=sources)
        fingerprint = normalize(fingerprint_text)[:60]
        for ticket in candidates:
            if ticket.id in exclude_ids:
                continue
            created_at = ticket.created_at
            if created_at is None:
                continue
            # Normalize to UTC-aware before comparing.
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            if created_at < cutoff:
                continue

            # Classify candidate by state.
            state = ticket.state
            if state == State.ERRORED:
                # Fix attempt failed — let a fresh draft retry.
                continue
            if state == State.CLOSED:
                # Was it ever DONE? If yes, treat as merged-then-closed
                # (a match). If no, it was declined-as-noise; skip.
                history = service.history(ticket.id)
                if not any(ev.state == State.DONE for ev in history):
                    continue
                # else: fall through, this is a match-eligible candidate.
            # DONE or any non-terminal (DRAFT/READY/IMPLEMENTING/etc.)
            # falls through here as a match-eligible candidate.

            # File-path substring check (body).
            if target_files:
                body = Workspace(
                    settings.workspaces_dir_for(ticket.board_id or board_id),
                    ticket.id,
                ).read_description()
                for path in target_files:
                    if path and path in body:
                        return ticket

            # Fingerprint check (normalized title).
            if fingerprint and fingerprint in normalize(ticket.title):
                return ticket
        return None
    except Exception:  # noqa: BLE001 — best-effort dedup
        log.exception("dedup: find_prior_matching_ticket failed")
        return None
