"""Recurrence detection for ci_fix-spawned dependency tickets.

An out-of-scope CI failure that was already fixed (and then reintroduced,
e.g. by a Dependabot bump undoing a version downgrade) used to spawn a
fresh, context-free fix ticket each time — robotsix-ui fixed the same
``npm ci`` ERESOLVE (typescript vs typescript-eslint peer range) four
times. The check-name fingerprint (``ci_fp:``) does not catch this: the
failing check names differ between recurrences. This module derives a
*failure signature* from the log itself, stores it as a ``ci_sig:``
label, and finds prior closed tickets carrying it so the new ticket can
demand a recurrence guard on top of the immediate fix.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..core.models import SourceKind, Ticket
from ..core.states import State
from .base import StageContext
from .dependency_fix import _parse_labels

RECURRENCE_WINDOW_DAYS = 90

_ERESOLVE_RE = re.compile(r"\bERESOLVE\b")
# ``npm error Found: typescript@7.0.2`` (npm >=10) / ``npm ERR! Found: …``.
_FOUND_RE = re.compile(r"Found:\s+((?:@[\w.-]+/)?[\w.-]+)@")
# ``npm error peer typescript@">=4.8.4 <6.1.0" from typescript-eslint@8.1.0``
_PEER_FROM_RE = re.compile(r"\bpeer\s[^\n]*?\sfrom\s+((?:@[\w.-]+/)?[\w.-]+)@")


def extract_failure_signature(failing_summary: str) -> str | None:
    """Return a normalised failure signature for *failing_summary*, or ``None``.

    The signature is the failing step plus a version-free error key, so
    the same root conflict hashes identically across recurrences no
    matter which check surfaced it or which versions were involved.
    Currently recognises npm ``ERESOLVE`` peer-dependency conflicts::

        npm ci ERESOLVE typescript/typescript-eslint
    """
    if not failing_summary or not _ERESOLVE_RE.search(failing_summary):
        return None
    found = _FOUND_RE.search(failing_summary)
    peer_from = _PEER_FROM_RE.search(failing_summary)
    if found is None or peer_from is None:
        return None
    step = "npm ci" if re.search(r"\bnpm ci\b", failing_summary) else "npm"
    return f"{step} ERESOLVE {found.group(1)}/{peer_from.group(1)}"


def signature_label(signature: str) -> str:
    """Return the ``ci_sig:`` ticket label for *signature*."""
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:16]
    return f"ci_sig:{digest}"


@dataclass(frozen=True)
class PriorFix:
    """A closed ci_fix_dependency ticket carrying the same signature."""

    ticket_id: str
    branch: str | None


def find_prior_fixes(
    ctx: StageContext,
    label: str,
    board_id: str,
    *,
    window_days: int = RECURRENCE_WINDOW_DAYS,
    now: datetime | None = None,
) -> list[PriorFix]:
    """Return closed ci_fix_dependency tickets on *board_id* labelled *label*.

    Only tickets created within the last *window_days* count; most
    recent first.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(days=window_days)
    candidates: list[Ticket] = ctx.service.recent_tickets(
        limit=200, sources=[SourceKind.CI_FIX_DEPENDENCY], board_id=board_id
    )
    prior: list[PriorFix] = []
    for cand in candidates:
        if cand.state not in (State.CLOSED, State.DONE):
            continue
        created = cand.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if created < cutoff:
            continue
        if label not in _parse_labels(cand.labels):
            continue
        prior.append(PriorFix(ticket_id=cand.id, branch=cand.branch))
    return prior


def format_recurrence_section(signature: str, prior: list[PriorFix]) -> str:
    """Render the recurrence block appended to the new fix ticket."""
    refs = "".join(
        f"\n- {p.ticket_id}" + (f" (PR from branch `{p.branch}`)" if p.branch else "")
        for p in prior
    )
    return (
        "\n## Recurrence\n\n"
        f"**Failure signature:** `{signature}` — this exact failure was "
        f"already fixed {len(prior)} time(s) in the last "
        f"{RECURRENCE_WINDOW_DAYS} days:{refs}\n\n"
        "**Required change area (additional):** prevent recurrence "
        "(e.g. dependabot ignore/group, version pin with comment) in "
        "addition to fixing the immediate failure.\n"
    )
