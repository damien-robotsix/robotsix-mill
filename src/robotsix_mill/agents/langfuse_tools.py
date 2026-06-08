"""Shared Langfuse tool factories for agents that need read-only access to
Langfuse trace data.

Used by both the answer agent and the refine agent.  The four simple tools
(``langfuse_session_summary``, ``langfuse_list_traces``,
``langfuse_trace_detail``, ``langfuse_session_cost``) do a single Langfuse
API call each.  The heavier ``langfuse_inspect_trace`` tool delegates to the
trace-inspector sub-agent for deep analysis.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Settings

log = logging.getLogger(__name__)


def _make_session_cost_tool(settings: Settings, repo_config=None):
    """Create the ``langfuse_session_cost`` tool closure.

    When *repo_config* is not ``None``, its Langfuse credentials are
    forwarded to the client call.
    """

    def langfuse_session_cost(session_id: str) -> str:
        """Fetch the total USD cost for a Langfuse session by its ID.
        Returns the cost as a dollar string (e.g. "$1.2345")."""
        from ..langfuse.client import session_cost

        kwargs: dict = {}
        if repo_config is not None:
            kwargs["repo_config"] = repo_config
        cost = session_cost(settings, session_id, **kwargs)
        return f"${cost:.4f}"

    return langfuse_session_cost


def _make_session_summary_tool(settings: Settings, repo_config=None):
    """Create the ``langfuse_session_summary`` tool closure.

    When *repo_config* is not ``None``, its Langfuse credentials are
    forwarded to the client call.
    """

    def langfuse_session_summary(session_id: str) -> str:
        """Fetch a structured summary of all traces in a Langfuse
        session: per-stage cost, latency, observation counts, plus any
        warnings/errors. Returns a Markdown text block."""
        from ..langfuse.client import fetch_session_summary

        kwargs: dict = {}
        if repo_config is not None:
            kwargs["repo_config"] = repo_config
        summary = fetch_session_summary(settings, session_id, **kwargs)
        if summary is None:
            return (
                f"No Langfuse data found for session {session_id} "
                f"(tracing may be unconfigured)"
            )
        return summary

    return langfuse_session_summary


def _make_list_traces_tool(settings: Settings, repo_config=None):
    """Create the ``langfuse_list_traces`` tool closure.

    When *repo_config* is not ``None``, its Langfuse credentials are
    forwarded to the client call.
    """

    def langfuse_list_traces(session_id: str) -> str:
        """List all trace IDs for a Langfuse session. Returns one trace
        per line with its name, timestamp, and cost."""
        from ..langfuse.client import _langfuse_api_get

        kwargs: dict = {}
        if repo_config is not None:
            kwargs["repo_config"] = repo_config
        data = _langfuse_api_get(
            settings,
            "/api/public/traces",
            params={"sessionId": session_id, "limit": 100},
            **kwargs,
        )
        if data is None:
            return "Langfuse unavailable or tracing not configured"
        traces = data.get("data", [])
        if not traces:
            return f"No traces found for session {session_id}"
        lines = []
        for t in traces:
            cost = t.get("totalCost") or 0
            lines.append(
                f"{t['id']}  {t.get('name', '?')}  "
                f"{t.get('timestamp', '')}  ${float(cost):.4f}"
            )
        return "\n".join(lines)

    return langfuse_list_traces


def _make_trace_detail_tool(settings: Settings, repo_config=None):
    """Create the ``langfuse_trace_detail`` tool closure.

    When *repo_config* is not ``None``, its Langfuse credentials are
    forwarded to the client call.
    """

    def langfuse_trace_detail(trace_id: str) -> str:
        """Fetch the full detail of a single Langfuse trace by its ID.
        Returns a JSON-like summary of the trace's observations."""
        from ..langfuse.client import fetch_trace_detail

        kwargs: dict = {}
        if repo_config is not None:
            kwargs["repo_config"] = repo_config
        detail = fetch_trace_detail(settings, trace_id, **kwargs)
        if detail is None:
            return f"No trace found for ID {trace_id}"
        obs = detail.get("observations") or []
        obs_summary: dict[str, int] = {}
        for o in obs:
            level = o.get("level", "DEFAULT")
            obs_summary[level] = obs_summary.get(level, 0) + 1
        lines = [
            f"trace: {detail.get('name', '?')}",
            f"id: {detail.get('id')}",
            f"timestamp: {detail.get('timestamp')}",
            f"cost: ${float(detail.get('totalCost') or 0):.4f}",
            f"latency: {float(detail.get('latency') or 0):.1f}s",
            f"observations: {len(obs)} "
            f"({', '.join(f'{k}={v}' for k, v in sorted(obs_summary.items()))})",
        ]
        return "\n".join(lines)

    return langfuse_trace_detail


def _build_langfuse_tools(settings: Settings, repo_config=None):
    """Create Langfuse read-only tools as closures capturing settings
    and an optional per-repo *repo_config*.

    When *repo_config* is not ``None`` its Langfuse credentials are
    used; otherwise the global :class:`Secrets` singleton fallback is
    used.

    Returns four callable closures with the same signatures and behaviour
    as the original ``answering.py:_build_langfuse_tools``.  The tool
    names are ``langfuse_session_cost``, ``langfuse_session_summary``,
    ``langfuse_list_traces``, and ``langfuse_trace_detail``.
    """
    return [
        _make_session_cost_tool(settings, repo_config=repo_config),
        _make_session_summary_tool(settings, repo_config=repo_config),
        _make_list_traces_tool(settings, repo_config=repo_config),
        _make_trace_detail_tool(settings, repo_config=repo_config),
    ]


def make_langfuse_inspect_tool(settings: Settings, repo_dir: Path | None = None):
    """Build the ``langfuse_inspect_trace`` tool closure.

    When *repo_dir* is provided, the trace-inspector sub-agent gets
    read-only repo tools (``read_file``, ``list_dir``, ``run_command``,
    ``explore``) so its findings can be grounded in the actual code.
    When ``None``, the sub-agent runs tool-less (same as the existing
    retrospect ``trace_inspect`` path).
    """

    def langfuse_inspect_trace(trace_id: str) -> str:
        """Deep-inspect a single Langfuse trace by ID.

        Fetches the full observation tree and delegates to a
        trace-inspector sub-agent that analyses tool errors, agent
        limitations, and optimisation opportunities.  When the repo
        clone is available, findings are grounded in the actual source
        code.

        Use this when you need to deep-dive into a specific trace for
        error patterns or behavioural analysis — it surfaces the root
        cause, not just the symptom.
        """
        from ..langfuse.client import fetch_trace_detail
        from .trace_inspector import run_trace_inspector, TraceFinding

        detail = fetch_trace_detail(settings, trace_id)
        if detail is None:
            return f"trace {trace_id} unavailable"

        trace_data = json.dumps(detail, default=str)
        result = run_trace_inspector(
            settings=settings,
            trace_data=trace_data,
            repo_dir=repo_dir,
        )

        parts: list[str] = [f"## trace {trace_id} inspection"]
        if result.error:
            parts.append(f"\n_inspector error: {result.error[:200]}_")
            return "\n".join(parts)

        # Group findings by category (same format as
        # make_trace_inspect_tool in trace_inspector.py).
        by_cat: dict[str, list[TraceFinding]] = {
            "tool_error": [],
            "agent_limitation": [],
            "optimization": [],
        }
        for f in result.findings:
            by_cat.setdefault(f.category, []).append(f)

        section_titles = {
            "tool_error": "Tool Errors",
            "agent_limitation": "Agent Limitations",
            "optimization": "Optimizations",
        }
        for cat, title in section_titles.items():
            items = by_cat.get(cat, [])
            if not items:
                continue
            parts.append(f"\n### {title}")
            for f in items:
                line = f"- {f.symptom}"
                if f.proposed_solution:
                    line += f"  _(fix: {f.proposed_solution[:200]})_"
                parts.append(line)

        if not result.findings:
            parts.append("\n(no issues found in this trace)")
        return "\n".join(parts)

    return langfuse_inspect_trace
