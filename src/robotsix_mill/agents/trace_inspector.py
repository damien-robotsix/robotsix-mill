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
    # When non-empty, surfaces an inspector-side failure to the caller
    # (e.g. "trace too large for the model context"). Empty findings
    # WITHOUT an error means a clean run with nothing notable; empty
    # findings WITH an error means the analysis didn't happen.
    error: str = ""


def _shrink_trace_data(trace_data: str, max_chars: int = 400_000) -> str:
    """Shrink a serialised Langfuse trace so a real implement-run trace
    (~15 MB raw, ~2-4 M tokens) fits the model's 1 M-token context.

    Strategy: parse the JSON, walk every observation, and cap each
    observation's verbose ``input``/``output`` to a head+tail snippet.
    Structural fields (id, name, type, level, parent, statusMessage,
    startTime/endTime, calculatedTotalCost, usage tokens) are preserved
    in full — those are what the inspector actually reasons about.

    If the trace isn't valid JSON or shrinking doesn't get us under
    ``max_chars``, the raw string is returned truncated head+tail —
    the model will see context but the prompt won't blow up.
    """
    import json as _json

    def _trim(v, head: int = 800, tail: int = 800) -> str:
        s = v if isinstance(v, str) else _json.dumps(v, default=str)
        if len(s) <= head + tail + 64:
            return s
        return s[:head] + f"\n…[trimmed {len(s) - head - tail} chars]…\n" + s[-tail:]

    try:
        trace = _json.loads(trace_data)
    except Exception:  # noqa: BLE001
        if len(trace_data) <= max_chars:
            return trace_data
        return (
            trace_data[: max_chars // 2]
            + f"\n…[trimmed {len(trace_data) - max_chars} chars]…\n"
            + trace_data[-max_chars // 2:]
        )

    obs = trace.get("observations") or []
    for o in obs:
        for k in ("input", "output", "metadata"):
            if k in o and o[k] is not None:
                o[k] = _trim(o[k])
        # usageDetails / costDetails can themselves be huge nested dicts
        for k in ("usageDetails", "costDetails"):
            if k in o and isinstance(o[k], (dict, list)):
                # keep totals, drop per-call breakdowns over a cap
                s = _json.dumps(o[k], default=str)
                if len(s) > 2000:
                    o[k] = f"[summarised {len(s)} chars — dropped breakdowns]"

    shrunk = _json.dumps(trace, default=str)
    if len(shrunk) <= max_chars:
        return shrunk
    # Still too big — last-resort head/tail truncation on the whole thing.
    return (
        shrunk[: max_chars // 2]
        + f"\n…[trimmed {len(shrunk) - max_chars} chars]…\n"
        + shrunk[-max_chars // 2:]
    )


def run_trace_inspector(
    *, settings: Settings, trace_data: str
) -> TraceInspectResult:
    """Analyse a single trace's full observation tree and return
    structured findings.

    On error, returns a result with ``error`` populated rather than
    silently returning an empty findings list — the previous behaviour
    made model-context-overflow indistinguishable from a clean
    no-findings run (e.g. the user couldn't tell *"trace was too big"*
    from *"trace was fine, nothing notable"*).
    """
    if not settings.openrouter_api_key:
        return TraceInspectResult(error="OPENROUTER_API_KEY is not set")

    # Shrink the payload BEFORE sending. A full implement-run trace
    # routinely serialises to 15+ MB / 2-4 M tokens; the model caps at
    # 1 M, and Langfuse's OTel ingest 413s on huge span attributes.
    trimmed = _shrink_trace_data(trace_data)

    # lazy: keep core import-light / the suite hermetic
    from pydantic_ai import Agent
    from pydantic_ai.providers.openrouter import OpenRouterProvider
    from pydantic_ai.usage import UsageLimits

    from .base import _close_async_client, timeout_http_client
    from .openrouter_cost import CostInstrumentedOpenRouterModel

    client = timeout_http_client(settings)
    model = CostInstrumentedOpenRouterModel(
        settings.trace_inspector_model,
        provider=OpenRouterProvider(
            api_key=settings.openrouter_api_key,
            http_client=client,
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
        f"<trace>\n{trimmed}\n</trace>"
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
        msg = str(e)
        # Common, recognisable failure modes get a user-readable hint.
        if "maximum context length" in msg.lower():
            return TraceInspectResult(
                error=f"trace too large for the model context even after "
                      f"trimming ({len(trimmed)} chars sent): {msg[:300]}"
            )
        return TraceInspectResult(error=msg[:500])
    finally:
        _close_async_client(client)
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
