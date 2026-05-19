"""Trace inspector sub-agent.

The retrospect agent's deep-analysis mode spawns this cheap sub-agent
per trace to inspect the full observation tree — raw model calls, tool
invocations, and their outcomes.  It surfaces systematic tool errors,
agent behavioural patterns (e.g. fix loops that never converge), and
cost/latency optimisation opportunities that the pre-computed summary
misses.

``run_trace_inspector`` is the single mockable seam — tests monkeypatch
it to inject synthetic results without a real LLM or Langfuse.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel

from ..config import Settings

log = logging.getLogger("robotsix_mill.trace_inspector")

_SYSTEM_PROMPT = """\
You are a trace inspector for an autonomous ticket pipeline. You are
given the FULL JSON observation tree of a single Langfuse trace. Your
job is to find systematic issues that a shallow summary would miss.

Analyse the trace and return a structured result with THREE lists:

1. **tool_errors**: tool invocations that failed or returned errors.
   Look at each observation — if `level` is "ERROR" or
   `statusMessage` indicates a failure, capture a one-line description
   (e.g. "run_command `pytest -q` returned exit code 1 — test failure").

2. **agent_limitations**: behavioural patterns that suggest the agent
   got stuck or was ineffective. Examples:
   - fix loops: the same tool is called repeatedly with the same or
     trivially-different arguments without converging
   - the agent retries a failed approach instead of pivoting
   - the agent makes no progress across multiple model calls
   Describe each pattern concisely with evidence (tool names + counts).

3. **optimizations**: cost, latency, or token-usage patterns worth
   acting on. Examples:
   - a model call with unusually high token usage relative to its work
   - redundant tool calls that could be cached or skipped
   - a stage whose latency is dominated by one slow operation
   Describe each opportunity concisely.

Be specific and evidence-based — reference observation IDs, tool names,
and counts.  If a category has nothing to report, return an empty list.
Never invent issues — only report what is actually in the trace data.
"""


class TraceInspectResult(BaseModel):
    tool_errors: list[str] = []
    agent_limitations: list[str] = []
    optimizations: list[str] = []


def run_trace_inspector(
    *, settings: Settings, trace_data: str
) -> TraceInspectResult:
    """Analyse a single trace's full observation tree and return
    structured findings.  Degrades to an empty result on error rather
    than raising — the caller must be able to continue across many
    traces."""
    if not settings.openrouter_api_key:
        return TraceInspectResult()

    # lazy: keep core import-light / the suite hermetic
    from pydantic_ai import Agent
    from pydantic_ai.providers.openrouter import OpenRouterProvider
    from pydantic_ai.usage import UsageLimits

    from .base import timeout_http_client
    from .openrouter_cost import CostInstrumentedOpenRouterModel

    model = CostInstrumentedOpenRouterModel(
        settings.trace_inspector_model,
        provider=OpenRouterProvider(
            api_key=settings.openrouter_api_key,
            http_client=timeout_http_client(settings),
        ),
    )
    agent = Agent(
        model=model,
        system_prompt=_SYSTEM_PROMPT,
        output_type=TraceInspectResult,
    )
    limits = UsageLimits(request_limit=3)  # one cheap call per trace
    prompt = (
        "Analyse the following Langfuse trace JSON for tool errors, "
        "agent limitations, and optimisation opportunities.\n\n"
        f"<trace>\n{trace_data}\n</trace>"
    )
    try:
        from .retry import call_with_retry

        result = call_with_retry(
            lambda: agent.run_sync(prompt, usage_limits=limits),
            settings=settings,
            what="trace_inspector",
        )
    except Exception as e:  # noqa: BLE001 — degrade, never break the caller
        log.warning("trace inspector failed: %s", e)
        return TraceInspectResult()
    return result.output


def make_trace_inspect_tool(settings: Settings):
    """Build the ``trace_inspect`` tool exposed to the retrospect agent
    in deep-analysis mode.  It fetches the full trace from Langfuse,
    delegates to ``run_trace_inspector``, and returns a formatted text
    summary — or a degradation message if the trace is unavailable."""

    def trace_inspect(trace_id: str) -> str:
        """Inspect a single Langfuse trace by ID. Returns a text summary
        of tool errors, agent limitations, and optimisation
        opportunities found in the full observation tree. Use this for
        EACH trace in the session when doing deep analysis."""
        from .. import langfuse_client

        detail = langfuse_client.fetch_trace_detail(settings, trace_id)
        if detail is None:
            return f"trace {trace_id} unavailable"

        # Serialise to compact JSON so the sub-agent gets the raw data.
        trace_data = json.dumps(detail, default=str)
        result = run_trace_inspector(settings=settings, trace_data=trace_data)

        parts: list[str] = [f"## trace {trace_id} inspection"]

        if result.tool_errors:
            parts.append("\n### Tool Errors")
            for e in result.tool_errors:
                parts.append(f"- {e}")

        if result.agent_limitations:
            parts.append("\n### Agent Limitations")
            for a in result.agent_limitations:
                parts.append(f"- {a}")

        if result.optimizations:
            parts.append("\n### Optimizations")
            for o in result.optimizations:
                parts.append(f"- {o}")

        if not result.tool_errors and not result.agent_limitations and not result.optimizations:
            parts.append("\n(no issues found in this trace)")

        return "\n".join(parts)

    return trace_inspect
