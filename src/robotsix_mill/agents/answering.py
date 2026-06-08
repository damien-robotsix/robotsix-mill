"""The answering agent: an investigative analyst that answers questions
using all available tools — repo exploration, web research, and Langfuse
trace data — and returns a free-form Markdown answer.

``run_answer_agent`` is the mockable seam — tests monkeypatch it.
"""

from __future__ import annotations

from pathlib import Path

from ..config import RepoConfig, Settings


def _build_langfuse_tools(settings: Settings, repo_config: RepoConfig | None = None):
    """Create Langfuse read-only tools as closures capturing settings
    and an optional per-repo *repo_config*. When *repo_config* is not
    ``None`` its Langfuse credentials are used; otherwise the global
    :class:`Secrets` singleton fallback is used."""

    def fetch_session_cost(session_id: str) -> str:
        """Fetch the total USD cost for a Langfuse session by its ID.
        Returns the cost as a dollar string (e.g. "$1.2345")."""
        from ..langfuse.client import session_cost

        cost = session_cost(settings, session_id, repo_config=repo_config)
        return f"${cost:.4f}"

    def fetch_session_summary(session_id: str) -> str:
        """Fetch a structured summary of all traces in a Langfuse
        session: per-stage cost, latency, observation counts, plus any
        warnings/errors. Returns a Markdown text block."""
        from ..langfuse.client import fetch_session_summary

        summary = fetch_session_summary(settings, session_id, repo_config=repo_config)
        if summary is None:
            return f"No Langfuse data found for session {session_id} (tracing may be unconfigured)"
        return summary

    def list_traces(session_id: str) -> str:
        """List all trace IDs for a Langfuse session. Returns one trace
        per line with its name, timestamp, and cost."""
        from ..langfuse.client import _langfuse_api_get

        data = _langfuse_api_get(
            settings,
            "/api/public/traces",
            params={"sessionId": session_id, "limit": 100},
            repo_config=repo_config,
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

    def fetch_trace_detail(trace_id: str) -> str:
        """Fetch the full detail of a single Langfuse trace by its ID.
        Returns a JSON-like summary of the trace's observations."""
        from ..langfuse.client import fetch_trace_detail

        detail = fetch_trace_detail(settings, trace_id, repo_config=repo_config)
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
            f"observations: {len(obs)} ({', '.join(f'{k}={v}' for k, v in sorted(obs_summary.items()))})",
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
    repo_config: RepoConfig | None = None,
) -> str:
    """Return a free-form Markdown answer string. When *repo_dir* is
    given the agent grounds its answer in that local clone via
    explore/read_file/list_dir/run_command. Always has web_research
    and Langfuse tools. Raises RuntimeError if no OpenRouter key is
    configured.

    When *repo_config* is provided, its Langfuse credentials are
    forwarded to the Langfuse tools so the agent queries the repo's
    own Langfuse project instead of the global one.
    """
    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close
    from .retry import run_agent

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent / "agent_definitions" / "answer.yaml"
    )

    tools: list = []
    if repo_dir is not None:
        from .explore import make_explore_tool
        from .fs_tools import build_fs_tools

        ro = [
            t
            for t in build_fs_tools(repo_dir, settings)
            if t.__name__ in ("read_file", "list_dir", "run_command")
        ]
        tools = [make_explore_tool(settings, repo_dir), *ro]

    # Langfuse read tools — always available
    langfuse_tools = _build_langfuse_tools(settings, repo_config=repo_config)
    tools.extend(langfuse_tools)

    agent = build_agent_from_definition(
        settings,
        definition,
        tools=tools,
        model_name=definition.model or settings.answer_model,
    )

    user_prompt = f"<title>{title}</title>\n<question>\n{question}\n</question>\n\nAnswer the question above. Cite all sources."

    try:
        result = run_agent(
            agent,
            lambda h: h.run_sync(user_prompt),
            settings=settings,
            what="answer",
        )
    finally:
        _safe_close(agent)
    return str(result.output).strip()
