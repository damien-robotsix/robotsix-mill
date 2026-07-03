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

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ConfigDict

from ..config import RepoConfig, Settings, get_secrets
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
   dominated by one slow operation. Optimization findings have a
   history of false positives — they rest on assumptions about code
   behaviour (prompt structure, cost drivers, control flow) that turn
   out to be wrong. Before filing one, you MUST verify the assumed
   code path (see "Verifying optimization hypotheses / mechanistic claims in root_cause" below).

## Verifying mechanistic claims in root_cause

The following rules apply to ALL finding categories (``tool_error``,
``agent_limitation``, ``optimization``) whenever ``root_cause`` or
``proposed_solution`` asserts a claim about how mill's code works —
what a tool returns, what a runner does, how a flag is computed, what
a function contains.  A wrong mechanistic claim wastes downstream
refine/implement budget regardless of category.  Before filing ANY
finding that makes such a claim:

- **Verify the assumed code path and control flow actually exist.**
  Use ``read_file`` / ``list_dir`` / ``explore`` / ``run_command``
  (e.g. ``git grep``) to open the relevant source and confirm the
  behaviour you are reasoning about — do not infer it from the trace
  alone. Cite the specific ``path/to/file.py:LINE`` locations you
  read in ``root_cause``.
- **Architectural / control-flow hypotheses require secondary code
  verification.** Any hypothesis that depends on how a loop, branch,
  early-return, retry, or merge behaves (e.g. "this loop
  short-circuits", "these paths merge", "all children must return X
  before proceeding") MUST be confirmed by reading the orchestration
  source. If you cannot open the code and confirm the control flow,
  do NOT file the finding as-is — downgrade it to ``confidence="low"``
  and prefix ``proposed_solution`` with ``REQUIRES_HUMAN_REVIEW:``,
  stating the unverified assumption explicitly so refine/a human can
  assess it rather than treating it as actionable.
- **Cite code locations for every root-cause claim.** A finding
  whose ``root_cause`` makes a mechanistic claim but names no
  ``path/to/file.py:LINE`` is not ready to file — either ground it in
  the code or drop it.

## Verifying statistical-signal flags

The Phase 1 classifier's ``cost_outlier``, ``observation_storm``, and
``tool_errors`` flags are statistical signals, not proof of a problem
— they have a history of false positives. Before filing ANY finding
whose evidence rests on one of these flags, you MUST cross-check the
trace's own per-observation ``model`` / ``usage`` /
``calculatedTotalCost`` data and rule out the benign explanations
below:

- **``cost_outlier``**: confirm the cost concentrates in genuinely
  expensive work, NOT a high-volume *cheap* model. A ``deepseek`` /
  flash sub-agent can rack up huge token counts while its
  ``calculatedTotalCost`` stays negligible — often because of a high
  prompt-cache hit rate. A high token count on a cheap model is **not**
  an anomaly. If the cost is plausibly explained by an expected recent
  change or a cheap model, do NOT file it as actionable.
- **``observation_storm``**: check whether the high observation count
  matches the genuine task scope (multi-file / multi-step work) and
  whether per-observation cost is low (cache-efficient execution). If
  the volume is proportionate to the scope, do NOT file it as
  actionable.
- **``tool_errors``**: distinguish retry / boundary patterns (e.g.
  ``read_file`` offset-boundary retries) and transient network errors
  from *structural* failures. Only file when the errors are structural.

Follow the SAME downgrade convention as the optimization gate: when
you cannot rule out a benign explanation, either drop the finding or
downgrade it to ``confidence="low"`` and prefix ``proposed_solution``
with ``REQUIRES_HUMAN_REVIEW:``, stating the unverified assumption
explicitly so downstream refine can detect and close it cheaply.

## Verifying error-mechanism hypotheses

For any ``tool_error`` or ``agent_limitation`` finding whose
``root_cause`` asserts *why* a failure occurred (a mechanism — e.g.
"X raises this error because of async/sync misuse", "this tool fails
because of Y"), do NOT attribute the failure to the nearest preceding
I/O or tool call merely because of proximity:

- **Trace the failing string, not the nearest call.** Take the literal
  error message / exception text from the trace and locate the code
  frame that actually *raises* it — use ``git grep`` of the error
  string, ``read_file``, or ``explore``. Cite the concrete
  ``path/to/file.py:LINE`` of the raising frame in ``root_cause``.
- **Pattern-match known bug classes before proposing a remedy.** Check
  the ``<memory>`` ledger and the repo for an already-solved instance
  of the same failure class, and prefer the repo's canonical fix.
  Worked example: the runtime error ``"This event loop is already
  running"`` is raised by ``pydantic-ai`` ``Agent.run_sync`` (=
  ``loop.run_until_complete``) invoked from a synchronous tool closure
  inside a live event loop — a **synchronous** ``httpx.Client`` never
  touches asyncio and cannot raise it; the canonical fix is the
  ``run_explore`` async pattern in ``agents/explore.py`` (make the tool
  ``async`` and ``await agent.run(...)``, or offload via
  ``asyncio.to_thread``).
- **When the mechanism cannot be confirmed from the code, do NOT
  commit to it.** Mirror the downgrade convention of the optimization
  and statistical-signal gates: either (a) state the ``symptom`` +
  the suspected *area* in ``root_cause`` *without* asserting an
  unverified mechanism, leaving mechanism discovery to refine; OR (b)
  keep the hypothesis but downgrade the finding to
  ``confidence="low"`` and prefix ``proposed_solution`` with
  ``REQUIRES_HUMAN_REVIEW:``, stating the unverified assumption
  explicitly. A confidently-wrong mechanism is more expensive
  downstream than an honest "suspected area, mechanism unconfirmed".

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
  BEFORE asserting any mechanistic claim about a file, tool, runner,
  or pipeline stage, use ``read_file`` / ``explore`` / ``git grep``
  to confirm it.  If you cannot open the code and confirm, set
  ``confidence`` to ``low`` and prefix ``proposed_solution`` with
  ``REQUIRES_HUMAN_REVIEW:``.  Cite ``path/to/file.py:LINE``.  If the
  evidence is in the trace only, say so — don't fabricate a code
  reference.
- ``proposed_solution``: a CONCRETE fix. Name files and (when known)
  the change pattern: "in ``agents/foo.py`` change X to Y because Z",
  "tighten the docstring on ``read_file`` to clarify N", "add an
  early-return guard before line M". The implement agent should be
  able to act on this directly.
- ``target_files``: the repo-relative file paths cited in
  ``proposed_solution`` (e.g. ``["src/robotsix_llmio/claude_sdk/wrapper.py"]``).
  Used by the trace-review runner's pre-filing dedup check to detect
  recurring findings against the same code locus. Empty list is
  acceptable when the finding has no code locus.
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

## Budget discipline

You have a ``request_limit`` budget — every tool call (read_file,
explore, run_command, list_dir) consumes one request from that limit.
When you exhaust the budget, the run terminates with an unhandled
``UsageLimitExceeded`` error and NO findings are filed.

- **Reserve at least 3 requests for the final synthesis turn.**
  Before making any tool call, ask yourself: "Do I have ≥ 3 requests
  remaining after this call?" If not, skip the tool call and produce
  your findings NOW with whatever evidence you already have.
- Prefer ``explore`` over serial ``read_file`` / ``run_command`` calls —
  one explore run can answer several questions at once.
- If you have read enough to form confidence="medium" findings,
  stop investigating and emit them. Detailed code-line verification
  is valuable but not at the cost of filing nothing.

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
    target_files: list[str] = Field(default_factory=list)
    confidence: Confidence = "medium"


class TraceInspectResult(BaseModel):
    """Structured result of a single trace deep-inspection pass.

    ``findings`` is the list of solution-bearing
    :class:`TraceFinding` items the inspector surfaced (empty for a
    clean trace), ``updated_memory`` is the agent's refreshed pattern
    ledger, and ``error`` is non-empty only when the analysis itself
    failed (e.g. the trace was too large for the model context) — which
    distinguishes a genuine no-findings run from one that never ran.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    findings: list[TraceFinding] = Field(default_factory=list)
    updated_memory: str = ""
    # When non-empty, surfaces an inspector-side failure to the caller
    # (e.g. "trace too large for the model context"). Empty findings
    # WITHOUT an error means a clean run with nothing notable; empty
    # findings WITH an error means the analysis didn't happen.
    error: str = ""


def _shrink_trace_data(trace_data: str, max_chars: int = 400_000) -> tuple[str, int]:
    """Shrink a serialised Langfuse trace so a real implement-run trace
    (~15 MB raw, ~2-4 M tokens) fits the model's 1 M-token context
    and return the observation count for budget scaling.

    Strategy: parse the JSON, walk every observation, and cap each
    observation's verbose ``input``/``output`` to a head+tail snippet.
    Structural fields (id, name, type, level, parent, statusMessage,
    startTime/endTime, calculatedTotalCost, usage tokens) are preserved
    in full — those are what the inspector actually reasons about.

    If the trace isn't valid JSON or shrinking doesn't get us under
    ``max_chars``, the raw string is returned truncated head+tail —
    the model will see context but the prompt won't blow up.

    Returns ``(shrunk_or_truncated_json: str, obs_count: int)``.
    The count is ``len(observations)`` for valid JSON, ``0`` for
    unparseable data.
    """
    import json as _json

    def _trim(v: Any, head: int = 800, tail: int = 800) -> str:
        s = v if isinstance(v, str) else _json.dumps(v, default=str)
        if len(s) <= head + tail + 64:
            return s
        return s[:head] + f"\n…[trimmed {len(s) - head - tail} chars]…\n" + s[-tail:]

    try:
        trace = _json.loads(trace_data)
    except Exception:  # noqa: BLE001
        if len(trace_data) <= max_chars:
            return trace_data, 0
        return (
            trace_data[: max_chars // 2]
            + f"\n…[trimmed {len(trace_data) - max_chars} chars]…\n"
            + trace_data[-max_chars // 2 :],
            0,
        )

    obs = trace.get("observations") or []
    obs_count = len(obs)

    if obs_count > 200:
        # For traces with observation storms (>200 observations), the
        # trimmed tree alone still blows the context window.  Strip
        # input/output/metadata entirely and keep only the structural
        # fields the inspector reasons about — id, type, level,
        # statusMessage, name, model, calculatedTotalCost, latency,
        # usageDetails, startTime, endTime.
        _KEEP_FIELDS = frozenset(
            {
                "id",
                "type",
                "level",
                "statusMessage",
                "name",
                "model",
                "calculatedTotalCost",
                "latency",
                "usageDetails",
                "startTime",
                "endTime",
            }
        )
        trace["observations"] = [
            {k: v for k, v in o.items() if k in _KEEP_FIELDS and v is not None}
            for o in obs
        ]
        shrunk = _json.dumps(trace, default=str)
        if len(shrunk) <= max_chars:
            return shrunk, obs_count
        return (
            shrunk[: max_chars // 2]
            + f"\n…[trimmed {len(shrunk) - max_chars} chars]…\n"
            + shrunk[-max_chars // 2 :],
            obs_count,
        )

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
        return shrunk, obs_count
    # Still too big — last-resort head/tail truncation on the whole thing.
    return (
        shrunk[: max_chars // 2]
        + f"\n…[trimmed {len(shrunk) - max_chars} chars]…\n"
        + shrunk[-max_chars // 2 :],
        obs_count,
    )


def _wrap_tools_with_error_limit(
    tools: list[Any],
    max_errors: int,
) -> list[Any]:
    """Wrap each tool with a shared error counter.

    When *max_errors* tool-call errors have been observed, the wrapper
    raises ``UsageLimitExceeded`` to terminate the agent run.  The
    pydantic-ai special exceptions ``UsageLimitExceeded`` and
    ``ModelRetry`` are NOT counted as errors — they pass through
    unchanged.

    Returns the original *tools* unchanged when *max_errors* <= 0.
    """
    if max_errors <= 0:
        return tools

    import functools
    import inspect as _inspect
    from collections.abc import Callable

    from pydantic_ai.exceptions import ModelRetry, UsageLimitExceeded

    state: dict[str, int] = {"errors": 0}

    def _make_wrapper(fn: Callable[..., Any]) -> Callable[..., Any]:
        if _inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    return await fn(*args, **kwargs)
                except UsageLimitExceeded, ModelRetry:
                    raise
                except Exception:
                    state["errors"] += 1
                    if state["errors"] > max_errors:
                        raise UsageLimitExceeded(
                            f"Error limit ({max_errors}) exceeded "
                            f"after {state['errors']} tool errors"
                        ) from None
                    raise

            return wrapper
        else:

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    return fn(*args, **kwargs)
                except UsageLimitExceeded, ModelRetry:
                    raise
                except Exception:
                    state["errors"] += 1
                    if state["errors"] > max_errors:
                        raise UsageLimitExceeded(
                            f"Error limit ({max_errors}) exceeded "
                            f"after {state['errors']} tool errors"
                        ) from None
                    raise

            return wrapper

    return [_make_wrapper(t) for t in tools]


def run_trace_inspector(
    *,
    settings: Settings,
    trace_data: str,
    repo_dir: Path | None = None,
    repo_config: RepoConfig | None = None,
    memory: str = "",
    started_at: datetime | None = None,
    classifier_flags: list[str] | None = None,
    request_limit_override: int | None = None,
) -> TraceInspectResult:
    """Analyse a single trace's full observation tree and return
    structured findings with proposed solutions.

    Two operating modes:

    - **Tool-less** (``repo_dir is None``): no code access, no tools,
      tight ``request_limit`` from
      ``trace_review_inspector_toolless_requests`` (default 3). Used
      by the retrospect agent's ``trace_inspect`` tool path —
      retrospect already runs in a tools-rich agent of its own, and
      the tool's contract is a quick text summary, not a deep dive.

    - **Tools-on** (``repo_dir`` provided): the inspector gets
      ``read_file``, ``list_dir``, ``run_command``, ``explore``, and
      ``parallel_explore`` — everything it needs to confirm a
      hypothesis in the code before writing a ``proposed_solution``.
      ``request_limit`` scales dynamically with the observation
      count: ``max(min_requests, min(max_requests, int(obs_count *
      requests_per_obs)))``, defaulting to 20–80 range at 0.1
      requests/obs.  When *obs_count* exceeds
      ``trace_review_inspector_max_obs_for_tools`` (default 200) the
      inspector falls back to the tool-less path even though
      *repo_dir* is supplied — a trace that large cannot be
      deep-verified in a bounded run.

    The optional ``memory`` is rendered into the prompt as
    ``<memory>...</memory>`` so the agent can avoid re-proposing what
    it already addressed, strengthen confidence on recurring patterns,
    and emit an updated ledger via ``result.updated_memory``.

    The optional ``started_at`` is the process start time — passed
    through from the trace-review runner so the LLM can reason about
    restart correlation when ``incomplete_trace`` +
    ``restart_correlated`` flags are present.

    The optional ``classifier_flags`` are the deterministic Phase-1
    flags that surfaced the trace — when non-empty they are rendered
    into the prompt as a ``classifier_flags`` section so the inspector
    knows *why* the trace was flagged and can apply the
    statistical-signal verification gate. ``None``/empty omits the
    section (the periodic retrospect tool-less call site leaves it
    unset).

    The optional ``repo_config`` carries the target repo's Langfuse
    read credentials so callers that fetch additional trace data on
    behalf of this sub-agent resolve the per-repo project rather than
    mill's global one. The inspector itself analyses the supplied
    ``trace_data`` and makes no further Langfuse calls, so this is
    threaded for credential-resolution consistency; the OpenRouter
    key (``get_secrets()``) stays global.

    **``request_limit_override``** (``int | None``, default ``None``)
    caps the tools-on ``request_limit`` from above — when set the
    effective limit is ``min(dynamic_limit, request_limit_override)``.
    The tool-less path is unaffected.  Intended for interactive tool
    call-sites (e.g. ``langfuse_inspect_trace``) that should do a
    quick, bounded confirmation rather than an unbounded deep audit.

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
    # Also returns the observation count so we can scale the request
    # budget without a second parse.
    trimmed, obs_count = _shrink_trace_data(trace_data)

    # lazy: keep core import-light / the suite hermetic
    from pydantic_ai import Agent, PromptedOutput
    from pydantic_ai.usage import UsageLimits

    from .base import _close_async_client, build_openrouter_model

    # Wire read-only fs tools + explore when repo_dir is provided.
    # NEVER include write_file/edit_file/delete_file: the inspector
    # is analysis-only, no side effects.
    from ._repo_tools import _build_repo_tools

    # Decide whether to enable code-access tools.  When a trace
    # exceeds the observation threshold, deep per-file code
    # verification is impractical — the explore fan-out is exactly
    # what exhausts the budget — so fall back to the cheap tool-less
    # summary path even when *repo_dir* is supplied.
    tools_on = (
        repo_dir is not None
        and obs_count <= settings.trace_review_inspector_max_obs_for_tools
    )

    # Build tools: pass repo_dir=None when tools are off so
    # _build_repo_tools returns an empty list (no explore /
    # parallel_explore / read_file / list_dir / run_command).
    tools = _build_repo_tools(
        repo_dir if tools_on else None,
        settings,
        include_parallel_explore=tools_on,
    )

    # Request budget: tools-on scales with observation count so the
    # inspector has room to read → reason → emit for complex traces;
    # tool-less (including the oversized-trace fallback) stays cheap.
    # When the classifier flagged an observation storm, the trace is
    # large and noisy — the inspector needs extra requests to explore
    # code paths before it can produce grounded findings.  Double the
    # already-computed budget (or raise the tool-less floor) so it
    # doesn't hit UsageLimitExceeded mid-analysis.
    _observation_storm = classifier_flags and any(
        f.startswith("observation_storm") for f in classifier_flags
    )

    if tools_on:
        request_limit = max(
            settings.trace_review_inspector_min_requests,
            min(
                settings.trace_review_inspector_max_requests,
                int(obs_count * settings.trace_review_inspector_requests_per_obs),
            ),
        )
        # Interactive tool call-sites can pass a tighter cap to keep
        # ad-hoc inspections cheap.  The override only lowers, never
        # raises — a caller cannot punch through the dynamic ceiling.
        if request_limit_override is not None:
            request_limit = min(request_limit, request_limit_override)
        if _observation_storm:
            request_limit = max(request_limit, 40)
        tool_calls_limit = settings.trace_review_max_tool_calls
        error_limit = settings.trace_review_max_errors
    else:
        request_limit = settings.trace_review_inspector_toolless_requests
        if _observation_storm:
            request_limit = max(request_limit, 10)
        tool_calls_limit = None
        error_limit = 0

    # Guard against runaway tool loops: cap total tool calls and
    # errors per trace.  pydantic-ai's built-in ``tool_calls_limit``
    # counts successful calls; a custom error-counter wrapper handles
    # the error budget.
    tools = _wrap_tools_with_error_limit(tools, max_errors=error_limit)

    model, client = build_openrouter_model(settings.trace_review_model_level)
    agent = Agent(
        model=model,
        system_prompt=_SYSTEM_PROMPT,
        # PromptedOutput tolerates providers that 404 on forced
        # tool_choice (DeepSeek's OpenRouter endpoint among others).
        output_type=PromptedOutput(TraceInspectResult),
        tools=tools,
    )
    limits = UsageLimits(request_limit=request_limit, tool_calls_limit=tool_calls_limit)
    flags_section = (
        section("classifier_flags", ", ".join(classifier_flags)) + "\n\n"
        if classifier_flags
        else ""
    )
    prompt = (
        section("memory", memory or "(empty — start a new ledger)")
        + "\n\n"
        + section(
            "process_info",
            f"Process started at: {started_at.isoformat() if started_at else 'unknown'}\n"
            f"Current time: {datetime.now(timezone.utc).isoformat()}",
        )
        + "\n\n"
        + flags_section
        + "Analyse the following Langfuse trace JSON for tool errors, "
        + "agent limitations, and optimisation opportunities. For each "
        + "finding, propose a CONCRETE solution grounded in the code "
        + "(use read_file / list_dir / explore to confirm hypotheses).\n\n"
        + section("trace", trimmed)
    )
    try:
        from .retry import run_agent

        result = run_agent(
            agent,
            lambda h: h.run_sync(prompt, usage_limits=limits),
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
