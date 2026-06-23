"""Shared spawn-or-reuse + wire-dependency + park-BLOCKED helper.

Extracted from :class:`~.ci_fix.CIFixStage._handle_out_of_scope` so that
other stages (implement baseline check, verify, review, merge) can
reuse the same idempotent pattern instead of dead-ending on ``BLOCKED``
without queuing a fix.
"""

from __future__ import annotations

import json
import logging

from ..core.models import SourceKind, Ticket, TicketKind
from ..core.states import State
from .base import Outcome, StageContext

log = logging.getLogger("robotsix_mill.stages.dependency_fix")


def _parse_labels(raw: str | None) -> list[str]:
    """Parse a JSON-encoded label list into a Python list of strings.

    Returns an empty list for ``None``, empty, or malformed input.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError, TypeError:
        return []


def _label_dedup(
    ctx: StageContext,
    dedup_labels: list[str],
    board_id: str,
) -> str | None:
    """Search recent tickets on *board_id* for a matching fingerprint label.

    Returns the id of the first open (non-terminal) ticket whose labels
    intersect with *dedup_labels*, or ``None`` if no match is found.
    """
    candidates: list[Ticket] = ctx.service.recent_tickets(limit=200, board_id=board_id)
    for cand in candidates:
        if cand.state in (State.CLOSED, State.DONE, State.ERRORED):
            continue
        cand_labels = _parse_labels(cand.labels)
        if any(label in cand_labels for label in dedup_labels):
            return cand.id
    return None


def _title_dedup(
    ctx: StageContext,
    source_kind: SourceKind,
    title: str,
) -> str | None:
    """Search proposals of *source_kind* for an open ticket with the same *title*."""
    proposals: list[Ticket] = ctx.service.recent_proposals_for(source_kind, limit=100)
    for cand in proposals:
        if cand.title == title and cand.state not in (State.CLOSED, State.DONE):
            return cand.id
    return None


def _create_fix(
    ctx: StageContext,
    *,
    title: str,
    description: str,
    source_kind: SourceKind,
    board_id: str | None,
    priority: bool,
    dedup_labels: list[str] | None,
) -> str:
    """Create a fresh fix ticket, store fingerprint labels, return its id."""
    fix = ctx.service.create(
        title=title,
        description=description,
        source=source_kind,
        kind=TicketKind.TASK,
        board_id=board_id,
        priority=priority,
    )
    fix_id = fix.id
    if dedup_labels:
        existing_labels: list[str] = []
        try:
            created = ctx.service.get(fix_id)
            if created is not None:
                existing_labels = _parse_labels(created.labels)
        except Exception:
            log.debug("could not read labels for new fix ticket %s", fix_id)
        ctx.service.set_labels(fix_id, existing_labels + dedup_labels)
    return fix_id


def spawn_dependency_fix(
    ticket: Ticket,
    ctx: StageContext,
    *,
    title: str,
    description: str,
    source_kind: SourceKind,
    block_reason_prefix: str,
    priority: bool = False,
    dedup_labels: list[str] | None = None,
) -> Outcome:
    """Spawn (or reuse) a dependency fix ticket, wire both ways, park BLOCKED.

    The caller provides a **deterministic** *title* so the spawn is
    idempotent across retries — the helper de-duplicates against
    still-open tickets from *source_kind* with the same title.

    When *dedup_labels* is a non-empty list, a label-based dedup
    search runs first (across all non-terminal tickets on the same
    board, regardless of source kind).  On a label match the existing
    ticket is reused; otherwise a fresh ticket is created and the
    fingerprint labels are stored on it via ``set_labels``.  The
    existing title-based dedup still runs as a fallback when
    *dedup_labels* is empty or no label match is found.

    Returns a ``BLOCKED`` :class:`Outcome` whose note includes the fix
    ticket id and the auto-resume guarantee.
    """
    board_id = ctx.repo_config.board_id if ctx.repo_config else None

    # --- label-based dedup ---
    fix_id: str | None = None
    if dedup_labels and board_id:
        fix_id = _label_dedup(ctx, dedup_labels, board_id)

    # --- title-based dedup (fallback) ---
    if fix_id is None:
        fix_id = _title_dedup(ctx, source_kind, title)

    # --- fresh create ---
    if fix_id is None:
        fix_id = _create_fix(
            ctx,
            title=title,
            description=description,
            source_kind=source_kind,
            board_id=board_id,
            priority=priority,
            dedup_labels=dedup_labels,
        )

    # Wire both directions: original depends on fix; fix auto-unblocks
    # original when it reaches DONE.
    ctx.service.set_depends_on(ticket.id, [fix_id])
    ctx.service.set_unblocks(fix_id, [ticket.id])

    # Link the two tickets via history notes (best-effort).
    try:
        ctx.service.add_history_note(
            ticket.id,
            f"parked pending dependency fix {fix_id}: {block_reason_prefix}",
        )
    except Exception:  # noqa: BLE001 — history note is best-effort
        log.warning("%s: failed to record dependency-fix park note", ticket.id)
    try:
        ctx.service.add_history_note(
            fix_id,
            f"spawned by {ticket.id}: {block_reason_prefix}",
        )
    except Exception:  # noqa: BLE001 — history note is best-effort
        log.warning("%s: failed to record dependency-fix spawn note", fix_id)

    return Outcome(
        State.BLOCKED,
        f"{block_reason_prefix}. Parked pending fix ticket {fix_id}. "
        "Auto-resumes when that fix reaches DONE.",
    )
