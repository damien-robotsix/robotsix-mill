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
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ..config import Settings, get_secrets
from .prompt_blocks import section

log = logging.getLogger("robotsix_mill.trace_inspector")

_SYSTEM_PROMPT = """\
You are a trace inspector for an autonomous ticket pipeline. You are
given the FULL JSON observation tree of a single Langfuse trace AND
read-only access to the repository that produced it. Your job is not
just to *describe* issues — it is to **propose concrete, code-grounded
solutions** that an implement agent could ship.

Three categories of finding:

1. **tool_error**: a tool invocation failed or returned an error. Look
   at each observation — if `level` is "ERROR" or `statusMessage`
   indicates a failure.
2. **agent_limitation**: behavioural patterns that show the agent got
   stuck or was ineffective. Fix loops (same tool, same args, no
   convergence), retries of a failed approach, no progress across
   model calls.
3. **optimization**: cost / latency / token-usage waste. Unusually
   high token usage for trivial work, redundant tool calls, a stage
   dominated by one slow operation.

## Phase 1 classifier flags

The trace may carry deterministic flags set by the pre-classifier.
Pay special attention to these combinations:

- **``incomplete_trace``**: the trace ended before a final synthesis
  step — the root span ``output`` is null, or the last observation is
  a tool call (not a chat generation).
- **``incomplete_trace`` + ``restart_correlated``**: the trace's
  latest timestamp falls within the process restart correlation window.
  The root cause is almost certainly a container restart (Docker
  restart, OOM kill, deployment roll) that cut the agent mid-flight —
  NOT an agent-loop bug. Your ``proposed_solution`` should focus on
  restart resilience (checkpointing, idempotent requeue, graceful
  shutdown) rather than agent-loop fixes.

## How to produce a *useful* finding

For each finding, output four fields:

- ``category``: one of "tool_error", "agent_limitation",
  "optimization".
- ``symptom``: WHAT is happening, evidence-based. Reference
  observation IDs, tool names, counts, token totals. One sentence,
  grounded in the trace.
- ``root_cause``: WHY it's happening, traced to the code if possible.
  Use ``read_file`` / ``list_dir`` / ``explore`` to look at the
  prompts, tool docstrings, retry logic, or output schemas involved.
  Cite ``path/to/file.py:LINE``. If the evidence is in the trace only,
  say so — don't fabricate a code reference.
- ``proposed_solution``: a CONCRETE fix. Name files and (when known)
  the change pattern: "in ``agents/foo.py`` change X to Y because Z",
  "tighten the docstring on ``read_file`` to clarify N", "add an
  early-return guard before line M". The implement agent should be
  able to act on this directly.
- ``confidence``: "high" if you both saw the symptom in the trace AND
  confirmed the cause in the code; "medium" if you have a strong
  hypothesis from trace evidence alone; "low" if it's a guess worth
  investigating.

## Memory

You are given a ``<memory>`` ledger of patterns and decisions from
your past inspections. Reference it:

- Skip findings the ledger says were already addressed (don't re-propose).
- Strengthen confidence when the ledger shows the same pattern
  recurring across multiple traces.
- After analysis, update the ledger via ``updated_memory``: record
  newly-observed patterns, mark any findings whose proposed solutions
  have likely already been merged (you can grep / read_file to check),
  and prune stale entries.

## Output discipline

- One finding per ROOT issue. Don't split one bug across three
  findings ("token usage high", "many tool calls", "stage is slow"
  describing the same underlying loop = one finding).
- An empty findings list is fine for a clean trace.
- Never invent issues. If you cannot ground a proposed solution in
  the trace+code, return the finding with confidence="low" and an
  honest "investigate this further" note, OR drop it.
- Return the updated memory verbatim in ``updated_memory``. If
  nothing changed, return the input memory unchanged.
"""


FindingCategory = Literal["tool_error", "agent_limitation", "optimization"]
Confidence = Literal["high", "medium", "low"]


class TraceFinding(BaseModel):
    """One actionable finding from a deep-review pass.

    Solution-bearing by design — ``proposed_solution`` is mandatory
    so the ``+ Ticket`` flow can file drafts whose body is something
    an implement agent can act on directly, not just a symptom string.
    """

    category: FindingCategory
    symptom: str
    root_cause: str
    proposed_solution: str
    confidence: Confidence = "medium"


class TraceInspectResult(BaseModel):
    findings: list[TraceFinding] = Field(default_factory=list)
    updated_memory: str = ""
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
            + trace_data[-max_chars // 2 :]
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
        + shrunk[-max_chars // 2 :]
    )


def run_trace_inspector(
    *,
    settings: Settings,
    trace_data: str,
    repo_dir: Path | None = None,
    memory: str = "",
    model_name: str | None = None,
    started_at: datetime | None = None,
) -> TraceInspectResult:
    """Analyse a single trace's full observation tree and return
    structured findings with proposed solutions.

    Two operating modes:

    - **Tool-less** (``repo_dir is None``): no code access, no tools,
      tight ``request_limit=3``. Used by the retrospect agent's
      ``trace_inspect`` tool path — retrospect already runs in a
      tools-rich agent of its own, and the tool's contract is a quick
      text summary, not a deep dive. Behaviour matches the legacy
      shape.

    - **Tools-on** (``repo_dir`` provided): the inspector gets
      ``read_file``, ``list_dir``, ``run_command``, and ``explore`` —
      everything it needs to confirm a hypothesis in the code before
      writing a ``proposed_solution``. ``request_limit`` bumps to 20
      so it has room to read → reason → emit. Used by the manual
      Deep Review surface.

    The optional ``memory`` is rendered into the prompt as
    ``<memory>...</memory>`` so the agent can avoid re-proposing what
    it already addressed, strengthen confidence on recurring patterns,
    and emit an updated ledger via ``result.updated_memory``.

    The optional ``started_at`` is the process start time — passed
    through from the trace-review runner so the LLM can reason about
    restart correlation when ``incomplete_trace`` +
    ``restart_correlated`` flags are present.

    On error, the result's ``error`` field is populated rather than
    returning a silent empty findings list — the previous behaviour
    made model-context-overflow indistinguishable from a clean
    no-findings run.
    """
    if not get_secrets().openrouter_api_key:
        return TraceInspectResult(error="OPENROUTER_API_KEY is not set")

    # Shrink the payload BEFORE sending. A full implement-run trace
    # routinely serialises to 15+ MB / 2-4 M tokens; the model caps at
    # 1 M, and Langfuse's OTel ingest 413s on huge span attributes.
    trimmed = _shrink_trace_data(trace_data)

    # lazy: keep core import-light / the suite hermetic
    from pydantic_ai import Agent, PromptedOutput
    from pydantic_ai.providers.openrouter import OpenRouterProvider
    from pydantic_ai.usage import UsageLimits

    from .base import _close_async_client, timeout_http_client
    from .openrouter_cost import CostInstrumentedOpenRouterModel

    # Wire read-only fs tools + explore when repo_dir is provided.
    # NEVER include write_file/edit_file/delete_file: the inspector
    # is analysis-only, no side effects.
    tools: list = []
    if repo_dir is not None:
        from .fs_tools import build_fs_tools
        from .explore import make_explore_tool

        ro = [
            t
            for t in build_fs_tools(repo_dir, settings)
            if t.__name__ in ("read_file", "list_dir", "run_command")
        ]
        tools = [make_explore_tool(settings, repo_dir), *ro]
    # Tool-less path stays cheap (3 reqs); tools-on path needs room
    # to read → reason → emit (20 reqs is generous but bounded).
    request_limit = 20 if repo_dir is not None else 3

    client = timeout_http_client(settings)
    model = CostInstrumentedOpenRouterModel(
        model_name or settings.trace_inspector_model,
        provider=OpenRouterProvider(
            api_key=get_secrets().openrouter_api_key,
            http_client=client,
        ),
    )
    agent = Agent(
        model=model,
        system_prompt=_SYSTEM_PROMPT,
        # PromptedOutput tolerates providers that 404 on forced
        # tool_choice (DeepSeek's OpenRouter endpoint among others).
        output_type=PromptedOutput(TraceInspectResult),
        tools=tools,
    )
    limits = UsageLimits(request_limit=request_limit)
    prompt = (
        section("memory", memory or "(empty — start a new ledger)")
        + "\n\n"
        + section(
            "process_info",
            f"Process started at: {started_at.isoformat() if started_at else 'unknown'}\n"
            f"Current time: {datetime.now(timezone.utc).isoformat()}",
        )
        + "\n\n"
        + "Analyse the following Langfuse trace JSON for tool errors, "
        + "agent limitations, and optimisation opportunities. For each "
        + "finding, propose a CONCRETE solution grounded in the code "
        + "(use read_file / list_dir / explore to confirm hypotheses).\n\n"
        + section("trace", trimmed)
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

        # Serialise to compact JSON. NB: retrospect's deep-analysis path
        # calls this without a repo_dir — that's intentional. Retrospect
        # is itself a tool-bearing agent; trace_inspect here just adds a
        # quick per-trace summary to the synthesis. The full Deep Review
        # surface uses run_trace_inspector directly with repo_dir set,
        # to get solution-bearing findings.
        trace_data = json.dumps(detail, default=str)
        result = run_trace_inspector(settings=settings, trace_data=trace_data)

        parts: list[str] = [f"## trace {trace_id} inspection"]
        if result.error:
            parts.append(f"\n_inspector error: {result.error[:200]}_")
            return "\n".join(parts)

        # Group findings by category so the rendered output mirrors the
        # legacy three-section format retrospect's prompt was written
        # for. Each finding renders its symptom; if a proposed_solution
        # exists we tack it on as a parenthetical, keeping per-line
        # density readable.
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

    return trace_inspect
