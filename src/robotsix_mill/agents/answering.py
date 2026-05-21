"""The answering agent: an investigative analyst that answers questions
using all available tools — repo exploration, web research, and Langfuse
trace data — and returns a free-form Markdown answer.

``run_answer_agent`` is the mockable seam — tests monkeypatch it.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Settings

SYSTEM_PROMPT = """\
You are an investigative analyst for a software project. Your job is to
answer questions thoroughly using ALL available tools. You produce a
single, self-contained Markdown answer — no JSON envelope, no splitting.

## Grounding
- Use `explore` (a scout returning concise paths/symbols/line-ranges,
  never whole files), `read_file`/`list_dir`, and `run_command` to
  inspect the ACTUAL codebase. Ground every answer in real file paths
  and evidence.
- Use `web_research` for external lookups: current APIs, library docs,
  version details, standards, best practices.

## Langfuse tools
You have READ-ONLY access to the project's own Langfuse tracing data:
- `fetch_session_cost(session_id)`: total USD cost for a session.
- `fetch_session_summary(session_id)`: traces grouped by stage with
  per-stage cost, latency, and observation subtotals.
- `list_traces(session_id)`: list all trace IDs in a session.
- `fetch_trace_detail(trace_id)`: full detail of a single trace.

Use these to answer questions about the mill's own operations: costs,
trace data, agent behaviour, session details.

## Answer quality
- Cite ALL sources: repo paths, URLs, trace IDs, session IDs.
- Be thorough but concise. No preamble — just the answer.
- If you cannot answer confidently with available tools, say so and
  explain what's missing.
"""


def _build_langfuse_tools(settings: Settings):
    """Create Langfuse read-only tools as closures capturing settings."""

    def fetch_session_cost(session_id: str) -> str:
        """Fetch the total USD cost for a Langfuse session by its ID.
        Returns the cost as a dollar string (e.g. "$1.2345")."""
        from ..langfuse_client import session_cost

        cost = session_cost(settings, session_id)
        return f"${cost:.4f}"

    def fetch_session_summary(session_id: str) -> str:
        """Fetch a structured summary of all traces in a Langfuse
        session: per-stage cost, latency, observation counts, plus any
        warnings/errors. Returns a Markdown text block."""
        from ..langfuse_client import fetch_session_summary

        summary = fetch_session_summary(settings, session_id)
        if summary is None:
            return f"No Langfuse data found for session {session_id} (tracing may be unconfigured)"
        return summary

    def list_traces(session_id: str) -> str:
        """List all trace IDs for a Langfuse session. Returns one trace
        per line with its name, timestamp, and cost."""
        from ..langfuse_client import _langfuse_api_get

        data = _langfuse_api_get(
            settings,
            "/api/public/traces",
            params={"sessionId": session_id, "limit": 100},
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
                f"{t['id']}  {t.get('name','?')}  "
                f"{t.get('timestamp','')}  ${float(cost):.4f}"
            )
        return "\n".join(lines)

    def fetch_trace_detail(trace_id: str) -> str:
        """Fetch the full detail of a single Langfuse trace by its ID.
        Returns a JSON-like summary of the trace's observations."""
        from ..langfuse_client import fetch_trace_detail

        detail = fetch_trace_detail(settings, trace_id)
        if detail is None:
            return f"No trace found for ID {trace_id}"
        # Return a compact but useful subset: name, timestamp, cost,
        # latency, and observation count + levels.
        obs = detail.get("observations") or []
        obs_summary = {}
        for o in obs:
            level = o.get("level", "DEFAULT")
            obs_summary[level] = obs_summary.get(level, 0) + 1
        lines = [
            f"trace: {detail.get('name', '?')}",
            f"id: {detail.get('id')}",
            f"timestamp: {detail.get('timestamp')}",
            f"cost: ${float(detail.get('totalCost') or 0):.4f}",
            f"latency: {float(detail.get('latency') or 0):.1f}s",
            f"observations: {len(obs)} ({', '.join(f'{k}={v}' for k,v in sorted(obs_summary.items()))})",
        ]
        return "\n".join(lines)

    return [
        fetch_session_cost,
        fetch_session_summary,
        list_traces,
        fetch_trace_detail,
    ]


def run_answer_agent(
    *,
    settings: Settings,
    title: str,
    question: str,
    repo_dir: Path | None = None,
) -> str:
    """Return a free-form Markdown answer string. When *repo_dir* is
    given the agent grounds its answer in that local clone via
    explore/read_file/list_dir/run_command. Always has web_research
    and Langfuse tools. Raises RuntimeError if no OpenRouter key is
    configured.
    """
    from .base import build_agent
    from .retry import call_with_retry

    tools: list = []
    if repo_dir is not None:
        from .explore import make_explore_tool
        from .fs_tools import build_fs_tools

        ro = [
            t for t in build_fs_tools(repo_dir, settings)
            if t.__name__ in ("read_file", "list_dir", "run_command")
        ]
        tools = [make_explore_tool(settings, repo_dir), *ro]

    # Langfuse read tools — always available
    langfuse_tools = _build_langfuse_tools(settings)
    tools.extend(langfuse_tools)

    agent = build_agent(
        settings,
        system_prompt=SYSTEM_PROMPT,
        tools=tools,
        web=True,  # web_research sub-agent for external lookups
        model_name=settings.answer_model,
        name="answer",
    )

    user_prompt = f"<title>{title}</title>\n<question>\n{question}\n</question>\n\nAnswer the question above. Cite all sources."

    result = call_with_retry(
        lambda: agent.run_sync(user_prompt),
        settings=settings, what="answer",
    )
    return str(result.output).strip()
