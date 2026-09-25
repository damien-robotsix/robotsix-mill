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
    exclude_id: str | None = None,
) -> str | None:
    """Search recent tickets on *board_id* for a matching fingerprint label.

    Returns the id of the first open (non-terminal) ticket whose labels
    intersect with *dedup_labels*, or ``None`` if no match is found.
    *exclude_id* (the ticket being parked) is never returned — a ticket
    must not dedup to itself.
    """
    candidates: list[Ticket] = ctx.service.recent_tickets(limit=200, board_id=board_id)
    for cand in candidates:
        if cand.id == exclude_id:
            continue
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
    exclude_id: str | None = None,
) -> str | None:
    """Search proposals of *source_kind* for an open ticket with the same *title*.

    *exclude_id* (the ticket being parked) is never returned — a ticket
    must not dedup to itself.
    """
    proposals: list[Ticket] = ctx.service.recent_proposals_for(source_kind, limit=100)
    for cand in proposals:
        if cand.id == exclude_id:
            continue
        if cand.title == title and cand.state not in (State.CLOSED, State.DONE):
            return cand.id
    return None


def _reaches_via_unblocks(ctx: StageContext, start: str, target: str) -> bool:
    """Return ``True`` if *target* is reachable from *start* by following
    ``unblocks`` edges (a path of length >= 1).

    Parking a ticket adds the edge ``fix --unblocks--> ticket``. That
    closes a cycle iff ``ticket`` already reaches ``fix`` in the unblocks
    graph, so callers check ``_reaches_via_unblocks(ctx, ticket, fix)``
    before wiring. Walks the graph via ``ctx.service.get`` and is robust
    to missing tickets and malformed/absent ``unblocks`` payloads.
    """
    seen: set[str] = set()
    frontier: list[str] = [start]
    while frontier:
        node = frontier.pop()
        if node in seen:
            continue
        seen.add(node)
        cur = ctx.service.get(node)
        raw = getattr(cur, "unblocks", None) if cur is not None else None
        try:
            nxt = json.loads(raw) if raw else []
        except json.JSONDecodeError, TypeError:
            nxt = []
        if not isinstance(nxt, list):
            continue
        for edge in nxt:
            if not isinstance(edge, str):
                continue
            if edge == target:
                return True
            frontier.append(edge)
    return False


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
    ctx.service.transition(
        fix_id, State.READY, note="Auto-enqueued from dependency fix creation"
    )
    return fix_id


def _existing_unblocks_of(ctx: StageContext, fix_id: str) -> list[str]:
    """Return the current ``unblocks`` list stored on *fix_id* (minus any
    self-reference), tolerating a missing ticket or malformed payload.
    """
    fix = ctx.service.get(fix_id)
    if fix is None or not fix.unblocks:
        return []
    try:
        parsed = json.loads(fix.unblocks)
    except json.JSONDecodeError, TypeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [t for t in parsed if isinstance(t, str) and t != fix_id]


def _cycle_refusal_outcome(
    ctx: StageContext,
    ticket: Ticket,
    fix_id: str,
    block_reason_prefix: str,
) -> Outcome | None:
    """Return a BLOCKED escalation :class:`Outcome` when parking *ticket* on
    *fix_id* would form a dependency cycle, else ``None``.

    Parking adds ``fix_id --unblocks--> ticket``; that closes a cycle iff
    ``ticket`` already reaches ``fix_id`` in the unblocks graph (the reused
    fix ticket is itself waiting on this one). Wiring the reverse edge would
    leave both BLOCKED forever, invisible to blocked-auto-resume (which
    reports a legitimate dependency park as ``not_matched``). Escalate for
    manual resolution instead of parking.
    """
    if not _reaches_via_unblocks(ctx, ticket.id, fix_id):
        return None
    try:
        ctx.service.add_history_note(
            ticket.id,
            f"refused to park on {fix_id}: would create a dependency "
            "cycle (deadlock); escalating for manual resolution",
        )
    except Exception:
        log.warning("%s: failed to record cycle-refusal note", ticket.id)
    return Outcome(
        State.BLOCKED,
        f"{block_reason_prefix}. Detected a dependency cycle with existing "
        f"fix ticket {fix_id} (it is already waiting on this ticket); "
        "refusing to park to avoid a deadlock. Needs manual resolution.",
    )


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
        fix_id = _label_dedup(ctx, dedup_labels, board_id, exclude_id=ticket.id)

    # --- title-based dedup (fallback) ---
    if fix_id is None:
        fix_id = _title_dedup(ctx, source_kind, title, exclude_id=ticket.id)

    # A reused (deduped) ticket already carries edges, so it is the only
    # kind that can close a cycle; a freshly created fix ticket cannot.
    reused = fix_id is not None

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

    # Refuse to create a mutual-dependency deadlock (only possible on a
    # deduped reuse — a fresh fix ticket has no edges).
    if reused:
        refusal = _cycle_refusal_outcome(ctx, ticket, fix_id, block_reason_prefix)
        if refusal is not None:
            return refusal

    # Wire both directions: original depends on fix; fix auto-unblocks
    # original when it reaches DONE.
    ctx.service.set_depends_on(ticket.id, [fix_id])

    # Merge with any existing unblocks so that every ticket parked on
    # the same fix ticket is auto-resumed when it completes — not just
    # the last caller to wire through spawn_dependency_fix.
    all_unblocks = [*_existing_unblocks_of(ctx, fix_id), ticket.id]
    ctx.service.set_unblocks(fix_id, all_unblocks)

    # Link the two tickets via history notes (best-effort).
    try:
        ctx.service.add_history_note(
            ticket.id,
            f"parked pending dependency fix {fix_id}: {block_reason_prefix}",
        )
    except Exception:
        log.warning("%s: failed to record dependency-fix park note", ticket.id)
    try:
        ctx.service.add_history_note(
            fix_id,
            f"spawned by {ticket.id}: {block_reason_prefix}",
        )
    except Exception:
        log.warning("%s: failed to record dependency-fix spawn note", fix_id)

    return Outcome(
        State.BLOCKED,
        f"{block_reason_prefix}. Parked pending fix ticket {fix_id}. "
        "Auto-resumes when that fix reaches DONE.",
    )
