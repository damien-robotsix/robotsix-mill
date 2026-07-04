"""Trace-health runner — checks Langfuse for unsessioned / unnamed traces.

A deterministic, no-LLM check: fetches all traces from the last 24h
via the Langfuse public API, counts those missing a ``sessionId`` or
``name``, and files a single draft ticket when orphans are found (with
dedup against existing open "trace-health" tickets).

Seam: tests monkeypatch ``list_all_traces_since`` from langfuse.client.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from ..config import RepoConfig, Settings
from ..core.db import session
from ..core.models import SourceKind, Ticket
from ..core.service import TicketService
from ..core.states import State
from ..langfuse.client import list_all_traces_since
from ..runtime.tracing import make_session_id

log = logging.getLogger("robotsix_mill.trace_health")


@dataclass
class TraceHealthResult:
    """Result of running a trace-health check."""

    draft_created: bool
    unsessioned_count: int
    total_traces: int
    window_start: str  # ISO 8601
    window_end: str  # ISO 8601
    name_missing_count: int = 0


def run_trace_health_check(repo_config: RepoConfig | None = None) -> TraceHealthResult:
    """Execute one trace-health check.

    Args:
        repo_config: Optional per-repo configuration for multi-repo
            serve. When provided, ticket creation is scoped to this repo.

    Returns a ``TraceHealthResult``.  May create a draft ticket
    (``source="trace-health"``) when unsessioned or unnamed traces
    are detected.
    """
    settings = Settings()

    if repo_config is None:
        raise ValueError(
            "run_trace_health_check: repo_config is required — "
            "configure at least one repo in config/repos.yaml."
        )
    service = TicketService(settings, board_id=repo_config.repo_id)
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=24)
    window_end = now
    from_ts = window_start.isoformat()

    session_id = make_session_id("trace-health")

    # 1. Short-circuit when tracing is not configured.
    if not settings.tracing_enabled:
        log.debug("tracing disabled — skipping trace-health check")
        return TraceHealthResult(
            draft_created=False,
            unsessioned_count=0,
            name_missing_count=0,
            total_traces=0,
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
        )

    # 2. Fetch all traces from the last 24h.
    traces = list_all_traces_since(settings, from_ts, repo_config=repo_config)

    # 3. Partition.
    unsessioned = [t for t in traces if not t.get("sessionId")]
    unsessioned_count = len(unsessioned)
    name_missing = [t for t in traces if not t.get("name")]
    name_missing_count = len(name_missing)
    total = len(traces)

    # 4. Nothing to alert about.
    if (unsessioned_count == 0 and name_missing_count == 0) or total == 0:
        return TraceHealthResult(
            draft_created=False,
            unsessioned_count=unsessioned_count,
            name_missing_count=name_missing_count,
            total_traces=total,
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
        )

    # 5. Dedup: skip if an open trace-health ticket already exists.
    with session(settings, repo_config.repo_id) as s:
        stmt = (
            select(Ticket)
            .where(Ticket.source == SourceKind.TRACE_HEALTH)
            .where(Ticket.state != State.CLOSED)
        )
        stmt = stmt.where(Ticket.board_id == repo_config.repo_id)
        existing = list(s.exec(stmt).all())
    if existing:
        log.info(
            "open trace-health ticket(s) already exist (%d) — skipping",
            len(existing),
        )
        return TraceHealthResult(
            draft_created=False,
            unsessioned_count=unsessioned_count,
            name_missing_count=name_missing_count,
            total_traces=total,
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
        )

    # 6. Build and file the draft ticket.
    if unsessioned_count > 0 and name_missing_count > 0:
        title = (
            f"Langfuse trace orphans detected — "
            f"{unsessioned_count} unsessioned, {name_missing_count} unnamed "
            f"/ {total} total"
        )
    elif unsessioned_count > 0:
        title = (
            f"Unsessoned Langfuse traces detected — "
            f"{unsessioned_count}/{total} traces lack session"
        )
    else:
        title = (
            f"Langfuse unnamed traces detected — "
            f"{name_missing_count}/{total} traces lack name"
        )

    unsessioned_examples = []
    for t in unsessioned[:5]:
        tid = t.get("id", "?")
        tname = t.get("name", "?")
        unsessioned_examples.append(f'- {tid}  "{tname}"')
    unsessioned_block = (
        "\n".join(unsessioned_examples) if unsessioned_examples else "(none)"
    )

    unnamed_examples = []
    for t in name_missing[:5]:
        tid = t.get("id", "?")
        unnamed_examples.append(f"- {tid}  (unnamed)")
    unnamed_block = "\n".join(unnamed_examples) if unnamed_examples else "(none)"

    description = (
        f"Window: {window_start.isoformat()} UTC → {window_end.isoformat()} UTC\n"
        f"\n"
        f"Unsessoned traces: {unsessioned_count}\n"
        f"{unsessioned_block}\n"
        f"\n"
        f"Unnamed traces: {name_missing_count}\n"
        f"{unnamed_block}\n"
        f"\n"
        f"Likely cause: sub-agents (explore, web_research, deep/test, coordinator)\n"
        f"not inheriting the ticket root span's session.id. Additionally,\n"
        f"sub-agent / periodic paths (explore, calendar-agent, completeness_check,\n"
        f"ci-failure) run in an execution context that doesn't carry the langfuse\n"
        f"session contextvar — the root span is unnamed and llmio can't recover a\n"
        f"name post-hoc. A fix ticket should be produced by the pipeline (this\n"
        f"ticket itself is just the alert).\n"
    )

    ticket = service.create(
        title,
        description,
        source=SourceKind.TRACE_HEALTH,
        origin_session=session_id,
    )
    log.info(
        "trace-health filed draft %s: %d unsessioned, %d unnamed / %d traces",
        ticket.id,
        unsessioned_count,
        name_missing_count,
        total,
    )
    return TraceHealthResult(
        draft_created=True,
        unsessioned_count=unsessioned_count,
        name_missing_count=name_missing_count,
        total_traces=total,
        window_start=window_start.isoformat(),
        window_end=window_end.isoformat(),
    )
