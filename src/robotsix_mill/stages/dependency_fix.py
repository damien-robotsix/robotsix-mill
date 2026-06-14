"""Shared spawn-or-reuse + wire-dependency + park-BLOCKED helper.

Extracted from :class:`~.ci_fix.CIFixStage._handle_out_of_scope` so that
other stages (implement baseline check, verify, review, merge) can
reuse the same idempotent pattern instead of dead-ending on ``BLOCKED``
without queuing a fix.
"""

from __future__ import annotations

import json
import logging

from ..core.models import SourceKind, Ticket
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
    except (json.JSONDecodeError, TypeError):
        return []


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

    fix_id: str | None = None

    # --- label-based dedup (new) ---
    if dedup_labels and board_id:
        candidates = ctx.service.recent_tickets(limit=200, board_id=board_id)
        for cand in candidates:
            if cand.state in (State.CLOSED, State.DONE, State.ERRORED):
                continue
            cand_labels = _parse_labels(cand.labels)
            if any(label in cand_labels for label in dedup_labels):
                fix_id = cand.id
                break

    # --- title-based dedup (existing fallback) ---
    if fix_id is None:
        for cand in ctx.service.recent_proposals_for(source_kind, limit=100):
            if cand.title == title and cand.state not in (State.CLOSED, State.DONE):
                fix_id = cand.id
                break

    if fix_id is None:
        fix = ctx.service.create(
            title=title,
            description=description,
            source=source_kind,
            kind="task",
            board_id=board_id,
            priority=priority,
        )
        fix_id = fix.id
        # Persist fingerprint labels on the fresh ticket.
        if dedup_labels:
            existing_labels: list[str] = []
            try:
                created = ctx.service.get(fix_id)
                if created is not None:
                    existing_labels = _parse_labels(created.labels)
            except Exception:  # noqa: BLE001 — best-effort
                pass
            ctx.service.set_labels(fix_id, existing_labels + dedup_labels)

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
