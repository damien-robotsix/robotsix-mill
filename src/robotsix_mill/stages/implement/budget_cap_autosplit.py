"""Budget-cap auto-split fallback for oversized implement tickets.

Checkpoint/resume lets most capped implement passes converge: the pass is
BLOCKED with an ``agent budget cap — resumable`` note, auto-resumed, and
continues from its checkpoint.  But a ticket whose scope is genuinely too
large for a single ``coordinator_max_tool_calls`` pass loops forever —
resume → cap → block → resume — never shipping.

This module is the fallback.  After a ticket has accumulated
``coordinator_budget_cap_autosplit_threshold`` budget-cap pauses *without*
completing, the implement stage splits the oversized parent into
independently-shippable child tickets by reusing the existing
epic-breakdown scope-split logic, and parks the parent as an epic so the
board stops re-queuing the same oversized pass.

Two guards keep a *spinning* agent (one that burns the whole budget without
producing any real change) from being split indefinitely:

* the stall guard blocks a no-progress pass with a *different* note, so such
  passes never accumulate budget-cap notes and never reach the threshold;
* the progress gate below refuses to split a ticket whose branch carries no
  committed/working-tree diff.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime
from pathlib import Path

from ...core.models import Ticket, TicketEvent, TicketKind
from ...core.states import State
from ..base import Outcome, StageContext

log = logging.getLogger("robotsix_mill.implement.autosplit")

# Substring shared by every budget-cap BLOCKED note the implement
# coordinator loop emits (``agent budget cap — resumable (move to READY):
# …``).  Counting history events whose note carries this marker yields the
# per-ticket budget-cap pause count the split threshold keys on.
BUDGET_CAP_NOTE_MARKER = "agent budget cap"

# Leading marker of the split outcome note — also the human-readable record
# that an auto-split happened on this parent.
AUTOSPLIT_NOTE_MARKER = "budget-cap auto-split"


def count_budget_cap_pauses(ctx: StageContext, ticket_id: str) -> list[datetime]:
    """Return the timestamps of every budget-cap BLOCKED pause recorded for
    *ticket_id*, oldest first.

    The count + timestamps are the per-ticket budget-cap termination state
    the split threshold reads; they live in the ticket's append-only history
    (one BLOCKED event per cap), so no separate bookkeeping field is needed.
    """
    events: list[TicketEvent] = []
    try:
        events = ctx.service.history(ticket_id, order="asc")
    except Exception:
        log.debug("autosplit: history read failed for %s", ticket_id, exc_info=True)
        return []
    return [
        ev.at
        for ev in events
        if ev.state is State.BLOCKED and BUDGET_CAP_NOTE_MARKER in (ev.note or "")
    ]


def _branch_has_progress(repo_dir: Path, _branch: str, target_branch: str) -> bool:
    """True when the ticket's branch carries real work — commits ahead of
    *target_branch* or an uncommitted diff.

    A ticket that keeps hitting the cap without ever producing a diff is a
    spinning agent, not an oversized scope: it must stay capped, not split
    (splitting would only spawn equally-spinning children).  Probe errors
    fail open (treat as progress) — the stall guard is the primary
    no-progress defence and we must not let a transient git hiccup suppress a
    legitimate split.
    """
    try:
        dirty = subprocess.run(
            ["git", "-C", str(repo_dir), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
        if dirty.stdout.strip():
            return True
        ahead = subprocess.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "rev-list",
                "--count",
                f"{target_branch}..HEAD",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if ahead.returncode != 0:
            # Could not compare (e.g. target ref missing) — fail open.
            return True
        n = ahead.stdout.strip()
        return bool(n and n.isdigit() and int(n) > 0)
    except Exception:
        log.debug("autosplit: progress probe failed", exc_info=True)
        return True


def _split_into_epic_children(
    ctx: StageContext, ticket: Ticket, spec: str
) -> list[str]:
    """Promote *ticket* to an epic and spawn independently-shippable children
    from its scope via the existing epic-breakdown logic.

    Returns the created child ids (empty when the breakdown yields no
    children — the caller then falls back to the normal block path rather
    than retiring the parent with nothing to show for it).
    """
    from ...agents.epic_breakdown import (
        plan_child_dependencies,
        run_epic_breakdown_agent,
    )

    epic_description = (spec or "").strip() or ticket.title
    breakdown = run_epic_breakdown_agent(
        settings=ctx.settings,
        epic_title=ticket.title,
        epic_description=epic_description,
    )
    child_titles = list(breakdown.child_titles)
    child_bodies = list(breakdown.child_bodies)
    if not child_titles:
        return []

    # Only now (breakdown succeeded) flip the parent to an epic, so a failed
    # breakdown leaves the parent untouched and still resumable.
    ctx.service.promote_to_epic(ticket.id)
    created: list[tuple[str, str, str]] = []
    for child_title, child_body in zip(child_titles, child_bodies, strict=False):
        child = ctx.service.create(
            title=child_title,
            description=child_body,
            kind=TicketKind.TASK,
            parent_id=ticket.id,
        )
        created.append((child.id, child_title, child_body))

    for child_id, deps in plan_child_dependencies(created).items():
        ctx.service.set_depends_on(child_id, deps)

    epic_body = (getattr(breakdown, "epic_body", "") or "").strip()
    if epic_body:
        try:
            ctx.service.add_history_note(
                ticket.id, "epic body revised by budget-cap auto-split breakdown"
            )
        except Exception:
            log.debug("autosplit: epic-body note failed", exc_info=True)

    return [cid for cid, _t, _b in created]


def maybe_autosplit_on_budget_cap(
    ctx: StageContext,
    ticket: Ticket,
    repo_dir: Path,
    branch: str,
    target_branch: str,
    spec: str,
) -> Outcome | None:
    """Fallback for a ticket that keeps exhausting the coordinator tool-call
    budget.

    Called from the implement stage's budget-cap handler *before* it blocks.
    When the ticket has hit the cap at least
    ``coordinator_budget_cap_autosplit_threshold`` times (this cap included)
    and shows real progress, it is split into epic children and this returns
    an ``Outcome(EPIC_OPEN, …)`` for the caller to emit instead of a BLOCKED
    outcome.  Returns ``None`` in every other case — the caller then blocks
    (and the ticket resumes) exactly as before.

    Idempotent: a parent already promoted to an epic, or one that already has
    children, is never split again, so repeated resume→cap events cannot
    spawn duplicate children.
    """
    settings_obj = getattr(ctx, "settings", None)
    threshold = int(
        getattr(settings_obj, "coordinator_budget_cap_autosplit_threshold", 0) or 0
    )
    if threshold <= 0:
        return None

    # The cap that triggered this call is not yet recorded in history; count
    # it alongside the prior pauses.
    prior = count_budget_cap_pauses(ctx, ticket.id)
    cap_count = len(prior) + 1
    if cap_count < threshold:
        return None

    # Idempotency: never re-split an already-split parent.
    current = ctx.service.get(ticket.id) or ticket
    if current.kind is TicketKind.EPIC:
        return None
    try:
        if ctx.service.list_children(ticket.id):
            return None
    except Exception:
        log.debug("autosplit: list_children failed for %s", ticket.id, exc_info=True)

    # Progress gate: a spinning agent with no diff stays capped, not split.
    if not _branch_has_progress(repo_dir, branch, target_branch):
        log.info(
            "autosplit: %s hit the budget cap %d× but its branch shows no "
            "progress — staying capped (not split)",
            ticket.id,
            cap_count,
        )
        return None

    try:
        child_ids = _split_into_epic_children(ctx, ticket, spec)
    except Exception:
        log.exception(
            "autosplit: %s epic-breakdown failed — blocking as usual", ticket.id
        )
        return None
    if not child_ids:
        log.warning(
            "autosplit: %s epic-breakdown produced no children — blocking as usual",
            ticket.id,
        )
        return None

    note = (
        f"{AUTOSPLIT_NOTE_MARKER}: exhausted the tool-call budget cap "
        f"{cap_count}× without completing; split into {len(child_ids)} "
        f"independently-shippable child ticket(s): {', '.join(child_ids)}"
    )
    log.info(
        "autosplit: %s split into %d children after %d budget-cap pauses",
        ticket.id,
        len(child_ids),
        cap_count,
    )
    return Outcome(State.EPIC_OPEN, note[:200])
