"""Tracing/observability helpers for the worker processing loop.

Holds the Langfuse breadcrumb writer and the root-span
attribute/input/output summary builders extracted from
``processing.py`` to keep that module's size manageable.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from ...core.models import Ticket
from ...stages import Outcome, StageContext
from ..tracing import langfuse_trace_url

log = logging.getLogger("robotsix_mill.worker")


def _post_trace_event(
    ctx: StageContext,
    ticket_id: str,
    trace_id: str | None,
    stage_name: str,
) -> None:
    """Append the post-stage Langfuse trace URL to the ticket's history.

    Previously this wrote a comment with ``author="mill"``, which
    contaminated the channel refine + implement read for reviewer
    feedback — agents saw the unreadable trace URL and asked the
    operator "what did the reviewer say?". Writing the same breadcrumb
    to ``TicketEvent.note`` instead keeps it visible to humans
    browsing the ticket (the drawer renders history-event notes as
    Markdown so the link stays clickable) without polluting the
    comment stream.

    No-op when *trace_id* is ``None`` or ``langfuse_trace_url`` can't
    build a URL (Langfuse unconfigured). Failures are logged at
    warning level and never propagate.
    """
    if trace_id is None:
        return
    repo_config = ctx.repo_config
    url = langfuse_trace_url(trace_id, repo_config=repo_config)
    if url is None:
        return
    note = f"🔍 [Trace: {stage_name}]({url})"
    try:
        ctx.service.add_history_note(ticket_id, note)
    except Exception:
        log.warning(
            "failed to post trace-link history event for %s (%s)",
            ticket_id,
            stage_name,
            exc_info=True,
        )


def _root_span_attributes(
    ticket: Ticket, stage_name: str, dispatch_counts: Counter[str]
) -> dict[str, str]:
    """Build span attributes for Langfuse searchability from ticket metadata.

    Returns string-keyed values only — OTel span attributes must be
    scalar strings, bools, ints, or floats.
    """
    return {
        "ticket.state": ticket.state.value,
        "ticket.kind": (
            ticket.kind.value if hasattr(ticket, "kind") and ticket.kind else ""
        ),
        "ticket.retry_attempt": str(getattr(ticket, "retry_attempt", 0)),
        "ticket.review_rounds": str(getattr(ticket, "review_rounds", 0)),
        "ticket.implement_cycles": str(getattr(ticket, "implement_cycles", 0)),
        "ticket.blocked_from": ticket.blocked_from or "",
        "ticket.paused_from": ticket.paused_from or "",
        "ticket.dispatch_count": str(dispatch_counts.get(stage_name, 0)),
        "ticket.source": ticket.source or "",
        "stage.name": stage_name,
    }


def _root_input_summary(
    ticket: Ticket, ticket_id: str, stage_name: str, dispatch_count: int = 0
) -> dict[str, Any]:
    """Build the input-summary dict attached to the Langfuse root span.

    Includes ticket identity, current state, retry/review counters,
    and a dispatch counter that serves as an early-loop-detection
    trigger — a stage re-running many times in one pass signals a
    potential runaway.
    """
    return {
        "ticket_id": ticket_id,
        "title": ticket.title,
        "state": ticket.state.value,
        "kind": (
            ticket.kind.value if hasattr(ticket, "kind") and ticket.kind else None
        ),
        "stage": stage_name,
        "source": ticket.source,
        "priority": bool(getattr(ticket, "priority", False)),
        "retry_attempt": getattr(ticket, "retry_attempt", 0),
        "last_transient_error": getattr(ticket, "last_transient_error", None),
        "review_rounds": getattr(ticket, "review_rounds", 0),
        "implement_cycles": getattr(ticket, "implement_cycles", 0),
        "blocked_from": getattr(ticket, "blocked_from", None),
        "paused_from": getattr(ticket, "paused_from", None),
        "dispatch_count": dispatch_count,
        "workspace_path": getattr(ticket, "workspace_path", None),
    }


def _root_output_summary(outcome: Outcome | None, ticket: Ticket) -> dict[str, Any]:
    """Build the output-summary dict attached to the Langfuse root span."""
    return {
        "next_state": outcome.next_state.value
        if outcome and outcome.next_state
        else None,
        "note": (outcome.note or "") if outcome else "",
        "no_op": bool(outcome and outcome.next_state == ticket.state),
    }
