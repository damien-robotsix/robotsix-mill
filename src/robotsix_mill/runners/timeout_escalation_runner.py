"""Timeout escalation runner — detects AWAITING_USER_REPLY tickets stuck
beyond a configurable threshold and escalates them to BLOCKED.

A deterministic, no-LLM pass: pure DB query + state transition.  No AI agent,
no pass_runner, no Langfuse tracing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlmodel import select

from ..core.db import session
from ..core.models import Ticket
from ..core.service import TicketService
from ..core.states import ASK_USER_MARKER, State
from ..notify import send_notification

if TYPE_CHECKING:
    from ..config import Settings

log = logging.getLogger("robotsix_mill.timeout_escalation")


def _boards_to_scan(settings: Settings) -> list[str]:
    """Return the list of board_ids to scan for stale tickets.

    Enumerates: registered repos, and any subdirectory under
    ``data_dir`` that contains a ``mill.db`` (catches per-repo DBs
    even when repos.yaml is absent).
    """
    from ..config import get_repos_config

    boards: list[str] = []
    # Registered repos from repos.yaml.
    try:
        for rc in get_repos_config().repos.values():
            if rc.repo_id and rc.repo_id not in boards:
                boards.append(rc.repo_id)
    except Exception:
        pass
    # Walk subdirectories for any per-repo DB file not already covered
    # (catches repos created at runtime without a repos.yaml entry).
    try:
        for child in sorted(settings.data_dir.iterdir()):
            if child.is_dir() and (child / "mill.db").exists():
                bid = child.name
                if bid not in boards:
                    boards.append(bid)
    except OSError:
        pass
    return boards


def run_timeout_escalation(settings: Settings) -> dict:
    """Execute one timeout-escalation pass.

    Scans across all known boards (per-repo DBs + legacy default DB)
    for AWAITING_USER_REPLY tickets stuck beyond the threshold.

    Returns a summary dict with keys ``escaped`` (count of tickets
    escalated) and ``skipped`` (count of stale-looking tickets skipped
    because of operator activity or already-blocked guard).
    """
    threshold = settings.timeout_escalation_threshold_seconds
    if threshold <= 0:
        log.info(
            "timeout_escalation: threshold=%ds — disabled, no-op",
            threshold,
        )
        return {"escaped": 0, "skipped": 0}

    boards = _boards_to_scan(settings)
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=threshold)

    escalated = 0
    skipped = 0

    for board_id in boards:
        service = TicketService(settings, board_id=board_id)

        # Fetch all AWAITING_USER_REPLY tickets older than the cutoff.
        with session(settings, board_id) as s:
            stmt = (
                select(Ticket)
                .where(Ticket.state == State.AWAITING_USER_REPLY)
                .where(Ticket.updated_at < cutoff)
            )
            candidates = list(s.exec(stmt).all())

        if not candidates:
            log.debug(
                "timeout_escalation: board=%r — no stale AWAITING_USER_REPLY tickets",
                board_id or "<default>",
            )
            continue

        for ticket in candidates:
            try:
                # AC4: guard against race — ticket may already be BLOCKED.
                if ticket.state is State.BLOCKED:
                    log.warning(
                        "timeout_escalation: %s already BLOCKED — skipping",
                        ticket.id,
                    )
                    skipped += 1
                    continue

                # AC3: check whether any [ASK_USER] thread has an operator reply.
                # If the thread has a child reply but closed_at IS NULL, the
                # operator HAS responded — skip escalation.
                comments = service.list_comments(ticket.id)
                ask_threads = [
                    c
                    for c in comments
                    if c.parent_id is None
                    and (c.body or "").startswith(ASK_USER_MARKER)
                ]
                if ask_threads:
                    # Collect all top-level ask-thread IDs.
                    ask_ids = {t.id for t in ask_threads}
                    has_reply = any(c.parent_id in ask_ids for c in comments)
                    if has_reply:
                        log.info(
                            "timeout_escalation: %s has operator reply in "
                            "[ASK_USER] thread — skipping",
                            ticket.id,
                        )
                        skipped += 1
                        continue

                # Calculate staleness for the comment.
                stale_delta = datetime.now(timezone.utc) - ticket.updated_at.replace(
                    tzinfo=timezone.utc
                )
                stale_days = max(1, stale_delta.days)

                # Build the escalation note (fits in a 200-char transition note).
                note = (
                    f"Escalated to BLOCKED after {stale_days}d awaiting user reply — "
                    f"no operator response"
                )[:200]

                # Build the system comment body with thread reference.
                thread_ref = ""
                if ask_threads:
                    thread_ids = ", ".join(f"#{t.id}" for t in ask_threads)
                    thread_ref = f" (thread(s) {thread_ids})"

                comment_body = (
                    f"This ticket was escalated to BLOCKED because the question has "
                    f"been awaiting a reply for {stale_days} day(s)."
                    f"{thread_ref}"
                )

                # Transition to BLOCKED (raises TransitionError if illegal, caught below).
                updated = service.transition(ticket.id, State.BLOCKED, note=note)
                # Add system comment.
                service.add_comment(ticket.id, comment_body, author="system")
                # Fire existing BLOCKED notification.
                send_notification(updated, State.BLOCKED, note, settings)
                log.info(
                    "timeout_escalation: %s -> BLOCKED (stale %d days)",
                    ticket.id,
                    stale_days,
                )
                escalated += 1

            except Exception:
                # AC4: catch TransitionError (BLOCKED->BLOCKED) + any other
                # per-ticket failure — log and continue.
                log.warning(
                    "timeout_escalation: failed to escalate %s",
                    ticket.id,
                    exc_info=True,
                )
                skipped += 1

    log.info(
        "timeout_escalation: pass complete — escalated=%d skipped=%d",
        escalated,
        skipped,
    )
    return {"escaped": escalated, "skipped": skipped}
