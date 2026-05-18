"""Trace-health runner — checks Langfuse for unsessioned traces.

A deterministic, no-LLM check: fetches all traces from the last 24h
via the Langfuse public API, counts those missing a ``sessionId``, and
files a single draft ticket when unsessioned traces are found (with
dedup against existing open "trace-health" tickets).

Seam: tests monkeypatch ``list_all_traces_since`` from langfuse_client.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from .config import Settings
from .core.db import session
from .core.models import Ticket
from .core.service import TicketService
from .core.states import State
from .langfuse_client import list_all_traces_since

log = logging.getLogger("robotsix_mill.trace_health")


@dataclass
class TraceHealthResult:
    """Result of running a trace-health check."""

    draft_created: bool
    unsessioned_count: int
    total_traces: int
    window_start: str   # ISO 8601
    window_end: str     # ISO 8601


def run_trace_health_check() -> TraceHealthResult:
    """Execute one trace-health check.

    Returns a ``TraceHealthResult``.  May create a draft ticket
    (``source="trace-health"``) when unsessioned traces are detected.
    """
    settings = Settings()
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=24)
    window_end = now
    from_ts = window_start.isoformat()

    # 1. Short-circuit when tracing is not configured.
    if not settings.tracing_enabled:
        log.debug("tracing disabled — skipping trace-health check")
        return TraceHealthResult(
            draft_created=False,
            unsessioned_count=0,
            total_traces=0,
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
        )

    # 2. Fetch all traces from the last 24h.
    traces = list_all_traces_since(settings, from_ts)

    # 3. Partition.
    unsessioned = [t for t in traces if not t.get("sessionId")]
    unsessioned_count = len(unsessioned)
    total = len(traces)

    # 4. Nothing to alert about.
    if unsessioned_count == 0 or total == 0:
        return TraceHealthResult(
            draft_created=False,
            unsessioned_count=unsessioned_count,
            total_traces=total,
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
        )

    # 5. Dedup: skip if an open trace-health ticket already exists.
    with session(settings) as s:
        stmt = (
            select(Ticket)
            .where(Ticket.source == "trace-health")
            .where(Ticket.state != State.CLOSED)
        )
        existing = list(s.exec(stmt).all())
    if existing:
        log.info(
            "open trace-health ticket(s) already exist (%d) — skipping",
            len(existing),
        )
        return TraceHealthResult(
            draft_created=False,
            unsessioned_count=unsessioned_count,
            total_traces=total,
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
        )

    # 6. Build and file the draft ticket.
    title = (
        f"Unsessoned Langfuse traces detected — "
        f"{unsessioned_count}/{total} traces lack session"
    )
    examples = []
    for t in unsessioned[:5]:
        tid = t.get("id", "?")
        tname = t.get("name", "?")
        examples.append(f"- {tid}  \"{tname}\"")
    examples_block = "\n".join(examples) if examples else "(none)"
    description = (
        f"Window: {window_start.isoformat()} UTC → {window_end.isoformat()} UTC\n"
        f"Unsessoned traces: {unsessioned_count} / {total} total\n"
        f"\n"
        f"Examples (up to 5):\n"
        f"{examples_block}\n"
        f"\n"
        f"Likely cause: sub-agents (explore, web_research, deep/test, coordinator)\n"
        f"not inheriting the ticket root span's session.id. A fix ticket should\n"
        f"be produced by the pipeline (this ticket itself is just the alert).\n"
    )

    service = TicketService(settings)
    ticket = service.create(title, description, source="trace-health")
    log.info(
        "trace-health filed draft %s: %d/%d traces unsessioned",
        ticket.id,
        unsessioned_count,
        total,
    )
    return TraceHealthResult(
        draft_created=True,
        unsessioned_count=unsessioned_count,
        total_traces=total,
        window_start=window_start.isoformat(),
        window_end=window_end.isoformat(),
    )
