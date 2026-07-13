"""Shared Langfuse tool factories for agents that need read-only access to
Langfuse trace data.

Used by both the answer agent and the refine agent.  The four simple tools
(``langfuse_session_summary``, ``langfuse_list_traces``,
``langfuse_trace_detail``, ``langfuse_session_cost``) do a single Langfuse
API call each.  The heavier ``langfuse_inspect_trace`` tool delegates to the
trace-inspector sub-agent for deep analysis.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import RepoConfig, Settings
    from .trace_inspector import TraceFinding

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

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="langfuse_session_cost",
            description=(
                "Fetch the total USD cost for a Langfuse session by its ID. "
                'Returns the cost as a dollar string (e.g. "$1.2345").'
            ),
            category="reporting",
            parameters={"session_id": "str"},
        )
    )

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

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="langfuse_session_summary",
            description=(
                "Fetch a structured summary of all traces in a Langfuse "
                "session: per-stage cost, latency, observation counts, plus "
                "any warnings/errors. Returns a Markdown text block."
            ),
            category="reporting",
            parameters={"session_id": "str"},
        )
    )

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

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="langfuse_list_traces",
            description=(
                "List all trace IDs for a Langfuse session. Returns one "
                "trace per line with its name, timestamp, and cost."
            ),
            category="reporting",
            parameters={"session_id": "str"},
        )
    )

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

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="langfuse_trace_detail",
            description=(
                "Fetch the full detail of a single Langfuse trace by its ID. "
                "Returns a JSON-like summary of the trace's observations."
            ),
            category="reporting",
            parameters={"trace_id": "str"},
        )
    )

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


def make_cost_inspect_tool(
    settings: Settings,
    repo_dir: Path | None = None,
    repo_config: RepoConfig | None = None,
):
    """Build the ``inspect_cost`` tool closure.

    When *repo_dir* is provided the tool is registered (repo-scoped,
    same gate as ``langfuse_inspect_trace``).  When *repo_config* is
    not ``None`` its Langfuse credentials are used for the read calls;
    otherwise the global :class:`Secrets` fallback applies.  The tool
    returns a
    compact per-trace cost breakdown with provider attribution so the
    refine agent can surface discrepancies like "openrouter traces
    show $0 while the session total is non-zero" without guessing
    from source code alone.
    """

    def inspect_cost(session_id: str) -> str:
        """Inspect the per-trace cost breakdown for a ticket/session.

        Returns a compact structured summary:
        - Session total cost
        - Per-trace list with name, cost, model/provider tag, and
          timestamp
        - Sum of per-trace costs (for cross-checking against total)
        - Flag when a discrepancy is detected (e.g. session total
          non-zero but individual traces report $0)

        Use sparingly — each call hits Langfuse and counts against
        your refine request cap.
        """
        from ..langfuse.client import session_cost, session_traces

        kwargs: dict = {}
        if repo_config is not None:
            kwargs["repo_config"] = repo_config
        total = session_cost(settings, session_id, **kwargs)
        traces = session_traces(settings, session_id, **kwargs)

        if traces is None:
            return (
                f"Langfuse unavailable or tracing not configured "
                f"for session {session_id}"
            )

        lines = [
            f"## cost breakdown for {session_id}",
            f"session total: ${total:.4f}",
            f"trace count: {len(traces)}",
            "",
        ]

        if not traces:
            if total > 0:
                lines.append(
                    "⚠️  DISCREPANCY: session total is non-zero "
                    "but no traces were returned — provider "
                    "attribution is unavailable."
                )
            else:
                lines.append("(no traces — session cost is $0.00)")
            return "\n".join(lines)

        trace_sum = 0.0
        lines.append("| name | cost | model | timestamp | trace_id |")
        lines.append("|------|------|-------|-----------|----------|")
        for t in traces:
            trace_sum += t["cost"]
            model = t.get("model") or "?"
            lines.append(
                f"| {t['name']} | ${t['cost']:.4f} | {model} "
                f"| {t['at']} | {t['trace_id']} |"
            )

        lines.append("")
        lines.append(f"sum of per-trace costs: ${trace_sum:.4f}")
        lines.append(f"session total:         ${total:.4f}")

        if abs(total - trace_sum) > 0.0001:
            lines.append(
                f"⚠️  DISCREPANCY: per-trace sum (${trace_sum:.4f}) "
                f"≠ session total (${total:.4f}) — "
                f"diff ${total - trace_sum:+.4f}"
            )

        zero_cost_traces = [t for t in traces if t["cost"] == 0.0]
        if zero_cost_traces and total > 0:
            names = ", ".join(t["name"] for t in zero_cost_traces)
            lines.append(
                f"⚠️  {len(zero_cost_traces)} trace(s) with $0.00 cost "
                f"despite non-zero session total: {names}"
            )

        return "\n".join(lines)

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="inspect_cost",
            description=(
                "Inspect the per-trace cost breakdown for a ticket/session. "
                "Returns a compact structured summary: session total cost, "
                "per-trace list with name/cost/model/timestamp, and flags "
                "discrepancies between per-trace and session totals."
            ),
            category="reporting",
            parameters={"session_id": "str"},
        )
    )

    return inspect_cost


def render_trace_findings(
    findings: list[TraceFinding],
    trace_id: str,
    error: str | None = None,
) -> str:
    """Render a list of TraceFinding objects as a Markdown inspection report."""
    parts: list[str] = [f"## trace {trace_id} inspection"]
    if error:
        parts.append(f"\n_inspector error: {error[:200]}_")
        return "\n".join(parts)
    by_cat: dict[str, list[TraceFinding]] = {
        "tool_error": [],
        "agent_limitation": [],
        "optimization": [],
    }
    for f in findings:
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
    if not findings:
        parts.append("\n(no issues found in this trace)")
    return "\n".join(parts)


def make_langfuse_inspect_tool(
    settings: Settings,
    repo_dir: Path | None = None,
    repo_config: RepoConfig | None = None,
):
    """Build the ``langfuse_inspect_trace`` tool closure.

    When *repo_dir* is provided, the trace-inspector sub-agent gets
    read-only repo tools (``read_file``, ``list_dir``, ``run_command``,
    ``explore``) so its findings can be grounded in the actual code.
    When ``None``, the sub-agent runs tool-less (same as the existing
    retrospect ``trace_inspect`` path).

    When *repo_config* is not ``None`` its Langfuse credentials are
    used to fetch the trace and resolve the inspector's read client;
    otherwise the global :class:`Secrets` fallback applies.
    """

    def _blocking(trace_id: str) -> str:
        """Synchronous body of langfuse_inspect_trace — runs on a worker
        thread via ``asyncio.to_thread`` so ``run_trace_inspector`` (which
        internally calls ``run_sync`` → ``run_until_complete``) executes
        safely in a thread with no active event loop."""
        from ..langfuse.client import fetch_trace_detail
        from .trace_inspector import run_trace_inspector

        kwargs: dict = {}
        if repo_config is not None:
            kwargs["repo_config"] = repo_config
        detail = fetch_trace_detail(settings, trace_id, **kwargs)
        if detail is None:
            return f"trace {trace_id} unavailable"

        trace_data = json.dumps(detail, default=str)
        result = run_trace_inspector(
            # Shares trace_review_model_level with the automated pass
            # (configurable via Settings, defaults to cheapest tier).
            settings=settings,
            trace_data=trace_data,
            repo_dir=repo_dir,
            request_limit_override=settings.trace_review_tool_request_limit,
            **kwargs,
        )

        return render_trace_findings(result.findings, trace_id, result.error or None)

    async def langfuse_inspect_trace(trace_id: str) -> str:
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
        return await asyncio.to_thread(_blocking, trace_id)

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="langfuse_inspect_trace",
            description=(
                "Deep-inspect a single Langfuse trace by ID. Fetches the "
                "full observation tree and delegates to a trace-inspector "
                "sub-agent that analyses tool errors, agent limitations, "
                "and optimisation opportunities."
            ),
            category="reporting",
            parameters={"trace_id": "str"},
        )
    )

    return langfuse_inspect_trace
