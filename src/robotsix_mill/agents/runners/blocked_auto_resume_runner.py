"""Blocked auto-resume runner — retries BLOCKED tickets whose block note says
the condition is resumable, once, after a cooldown.

A BLOCKED ticket waits for a human ``resume-blocked``.  Live (7 days to
2026-08-29) 77 blocks landed on 56 tickets and 31 were resumed by hand;
the notes were overwhelmingly infrastructure or budget shaped — "agent
error — resumable" (provider output retries, session limit), "ci fix agent
could not turn CI green within its iteration budget", stage timeouts,
"clone missing — resumable", "pr_urls.json corrupted — resumable".  The
operator's playbook for every one of them was: wait a bit, click resume.
This pass does that click, deterministically and bounded:

* only the LATEST ``BLOCKED`` history note decides (same rule as the
  upstream-CI recovery runner), and only when it matches one of
  ``settings.blocked_auto_resume_patterns`` and none of the hard excludes
  (spec-fingerprint blocks need a description change; upstream-CI parks
  have their own runner);
* only after the ticket has been BLOCKED for
  ``blocked_auto_resume_cooldown_seconds`` — an immediate retry of a
  provider failure just fails again;
* at most ``blocked_auto_resume_max_per_ticket`` times per ticket, counted
  from the ``[auto-resume`` comments the resume itself leaves behind, so
  a ticket that keeps re-blocking ends up with a human exactly as before.

No LLM, no forge calls: history reads + ``resume_blocked``.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ...core.models import TicketEvent
from ...core.service import TicketService
from ...core.states import State
from ...stages.ci_fix_helpers import UPSTREAM_CI_BLOCK_MARKER
from .timeout_escalation_runner import _boards_to_scan

if TYPE_CHECKING:
    from ...config import Settings

log = logging.getLogger("robotsix_mill.blocked_auto_resume")

AUTO_RESUME_MARKER = "[auto-resume"

# Never auto-resumed regardless of the pattern list: these need a human or
# another pass.
_HARD_EXCLUDES = (
    "spec unchanged",  # implement fingerprint guard — description must change
    UPSTREAM_CI_BLOCK_MARKER,  # upstream_ci_recovery_runner owns these
    "refusing to close",  # retrospect: PRs not merged
    "not merged",
)


def _latest_blocked_event(service: TicketService, ticket_id: str) -> TicketEvent | None:
    events: list[TicketEvent] = service.history(ticket_id, order="desc")
    for ev in events:
        if ev.state is State.BLOCKED:
            return ev
    return None


def _matches(note: str, patterns: list[str]) -> bool:
    head = note[:400]
    if any(x.lower() in head.lower() for x in _HARD_EXCLUDES):
        return False
    for pat in patterns:
        try:
            if re.search(pat, head, re.IGNORECASE):
                return True
        except re.error:
            log.warning("blocked_auto_resume: invalid pattern %r ignored", pat)
    return False


def _prior_auto_resumes(service: TicketService, ticket_id: str) -> int:
    comments: list[Any] = list(service.list_comments(ticket_id) or [])
    return len(
        [
            c
            for c in comments
            if str(getattr(c, "body", "") or "").startswith(AUTO_RESUME_MARKER)
        ]
    )


def _age_seconds(at: datetime, now: datetime) -> float:
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return (now - at).total_seconds()


def run_blocked_auto_resume(
    settings: Settings, *, now: datetime | None = None
) -> dict[str, Any]:
    """One auto-resume pass over every board's BLOCKED tickets.

    Returns counts: ``resumed``, ``cooling`` (matched, not old enough yet),
    ``budget_exhausted`` (matched, already auto-resumed the maximum number
    of times), ``not_matched``.
    """
    now = now or datetime.now(UTC)
    cooldown = max(0, int(settings.blocked_auto_resume_cooldown_seconds))
    max_per = max(0, int(settings.blocked_auto_resume_max_per_ticket))
    patterns = list(settings.blocked_auto_resume_patterns or [])
    counts = {"resumed": 0, "cooling": 0, "budget_exhausted": 0, "not_matched": 0}
    if not patterns or max_per == 0:
        return counts
    for board_id in _boards_to_scan(settings):
        service = TicketService(settings, board_id=board_id)
        try:
            blocked = service.list(state=State.BLOCKED)
        except Exception:
            log.exception("blocked_auto_resume: board=%r list failed", board_id)
            continue
        for t in blocked:
            ev = _latest_blocked_event(service, t.id)
            if ev is None or not t.blocked_from:
                counts["not_matched"] += 1
                continue
            note = ev.note or ""
            if not _matches(note, patterns):
                counts["not_matched"] += 1
                continue
            age = _age_seconds(ev.at, now)
            if age < cooldown:
                counts["cooling"] += 1
                continue
            prior = _prior_auto_resumes(service, t.id)
            if prior >= max_per:
                counts["budget_exhausted"] += 1
                continue
            resume_note = (
                f"{AUTO_RESUME_MARKER} {prior + 1}/{max_per}] automatic retry after "
                f"{int(age // 60)} min BLOCKED (back to {t.blocked_from}); "
                f"block note was: {note[:200]}"
            )
            try:
                service.resume_blocked(t.id, note=resume_note)
            except Exception:
                log.exception("blocked_auto_resume: %s resume failed", t.id)
                continue
            counts["resumed"] += 1
            log.info(
                "blocked_auto_resume: %s resumed to %s after %d min (%d/%d): %s",
                t.id,
                t.blocked_from,
                int(age // 60),
                prior + 1,
                max_per,
                note[:120],
            )
    return counts
