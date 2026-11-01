"""Trace-review phase-1 deterministic classifier.

Split from ``trace_review_runner.py``.  Contains the memory-bounded,
streaming-friendly phase-1 code: per-batch baselines and the boolean
flag computation for a single trace — no LLM calls.  The runner's
phase-2 LLM inspection and orchestration import ``_classify_trace``,
``_compute_baselines``, ``_TraceFlags`` and the module constants from
here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ...config import Settings

# Public API consumed by ``trace_review_runner`` (phase-2 orchestration).
# Declared here so the module constants read by the runner are recognized
# as exports rather than flagged as unused globals.
__all__ = [
    "_CONFIDENCE_RANK",
    "_PER_OBS_COST_RE",
    "_SELF_REFERENTIAL_TRACE_NAMES",
    "_TraceFlags",
    "_classify_trace",
    "_compute_baselines",
    "_dedupe_tool_spans",
]

# ---------------------------------------------------------------------------
# Phase 1 — deterministic classifier
# ---------------------------------------------------------------------------

_TOOL_ERR_PATTERNS = re.compile(
    r"(\berror:|Traceback \(most recent call last\)|"
    r"UsageLimitExceeded|UnexpectedModelBehavior|"
    r"non-zero exit status)",
    re.IGNORECASE,
)

# Langfuse trace names emitted by trace_review's own pipeline.
# Traces with these names are silently skipped before phase-2 inspection.
_SELF_REFERENTIAL_TRACE_NAMES: frozenset[str] = frozenset({"trace_inspector"})

# Detects per-observation token-count symptoms that are not actionable on
# their own (covered by the cost-analyst periodic instead).
_PER_OBS_COST_RE = re.compile(
    r"\bobservation\b.{0,100}\bconsumed\b.{0,50}\d+\b.{0,30}\btokens?\b",
    re.IGNORECASE | re.DOTALL,
)

# Ordinal rank of inspector confidence, for the ``trace_review_min_confidence``
# floor. Higher = more grounded (symptom seen AND confirmed in code).
_CONFIDENCE_RANK: dict[str, int] = {"low": 1, "medium": 2, "high": 3}


@dataclass
class _Baselines:
    """Per-batch median thresholds against which a single trace is
    compared. ``None`` for either field means the batch was too small
    (< 3 traces) to compute a meaningful baseline — the relative flag
    is suppressed in that case.
    """

    cost_threshold: float | None
    obs_threshold: float | None
    cost_median: float | None
    obs_median: float | None


def _median(values: Sequence[float]) -> float:
    sorted_v = sorted(values)
    n = len(sorted_v)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2 == 1:
        return float(sorted_v[mid])
    return (sorted_v[mid - 1] + sorted_v[mid]) / 2.0


def _dedupe_tool_spans(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the SPAN twin of each TOOL observation.

    The tracing export records every tool invocation twice: a ``TOOL``
    observation and a ``SPAN`` with the same name and near-identical
    timings (one nested in the other). Counting both doubled
    ``observation_storm`` and ``repeated_tool`` counts. A SPAN is
    treated as a twin — and dropped — when an as-yet-unmatched TOOL
    observation shares its name and is either its parent/child or
    time-nested within it (or vice versa). Matching is 1:1, so
    genuinely distinct calls are never collapsed.
    """
    tools = [o for o in observations if o.get("type") == "TOOL"]
    if not tools:
        return observations
    unmatched = list(tools)
    dropped: set[int] = set()

    def _nested(a: dict[str, Any], b: dict[str, Any]) -> bool:
        a0, a1 = a.get("startTime") or "", a.get("endTime") or ""
        b0, b1 = b.get("startTime") or "", b.get("endTime") or ""
        if not (a0 and a1 and b0 and b1):
            return False
        return (a0 <= b0 and b1 <= a1) or (b0 <= a0 and a1 <= b1)

    for o in observations:
        if o.get("type") != "SPAN" or not o.get("name"):
            continue
        for t in unmatched:
            if t.get("name") != o.get("name"):
                continue
            linked = (o.get("id") and t.get("parentObservationId") == o.get("id")) or (
                t.get("id") and o.get("parentObservationId") == t.get("id")
            )
            if linked or _nested(o, t):
                unmatched.remove(t)
                dropped.add(id(o))
                break
    return [o for o in observations if id(o) not in dropped]


def _compute_baselines(
    traces: list[dict[str, Any]],
    obs_counts_per_trace: dict[str, int],
    settings: Settings,
) -> _Baselines:
    """Compute median × multiplier thresholds for cost and observation
    count across the entire *traces* batch. Returns ``None`` thresholds
    when the batch is too small to baseline (< 3 traces).

    *obs_counts_per_trace* maps a trace id to its observation *count*
    (not the observation payloads) so the whole batch can be baselined
    without holding every trace's observation tree in memory at once.
    """
    if len(traces) < 3:
        return _Baselines(None, None, None, None)
    costs = [float(t.get("totalCost") or 0.0) for t in traces]
    obs_counts = [obs_counts_per_trace.get(t.get("id") or "", 0) for t in traces]
    cost_med = _median(costs)
    obs_med = _median(obs_counts)
    # Guard against zero medians (every trace cost $0): without a
    # positive baseline the multiplier doesn't produce a meaningful
    # threshold; suppress the relative flag.
    return _Baselines(
        cost_threshold=(
            cost_med * settings.trace_review_cost_multiplier if cost_med > 0 else None
        ),
        obs_threshold=(
            obs_med * settings.trace_review_obs_multiplier if obs_med > 0 else None
        ),
        cost_median=cost_med,
        obs_median=obs_med,
    )


@dataclass
class _TraceFlags:
    """Boolean flags + counts a single trace produced in phase 1."""

    trace_id: str
    trace_name: str
    session_id: str
    total_cost: float
    flags: list[str] = field(default_factory=list)

    @property
    def flagged(self) -> bool:
        return bool(self.flags)


def _extract_trace_end_time(
    trace: dict[str, Any],
    observations: list[dict[str, Any]] | None,
) -> datetime | None:
    """Extract the latest timestamp from a trace for restart correlation.

    Priority: trace summary ``endTime`` → last observation ``endTime``
    → trace summary ``timestamp``.  Returns a timezone-aware datetime
    or ``None`` if no usable timestamp is found.
    """
    raw = trace.get("endTime") or ""
    if observations:
        sorted_obs = sorted(
            observations,
            key=lambda o: o.get("endTime") or o.get("startTime") or "",
        )
        raw = sorted_obs[-1].get("endTime") or raw
    raw = raw or trace.get("timestamp") or ""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError, TypeError:
        return None


def _classify_trace(
    trace: dict[str, Any],
    settings: Settings,
    observations: list[dict[str, Any]] | None = None,
    baselines: _Baselines | None = None,
    started_at: datetime | None = None,
) -> _TraceFlags:
    """Compute phase-1 flags from *trace* + its *observations*.

    *trace* is the summary dict from ``/api/public/traces`` (no
    observations). *observations* is the optional full observation
    tree from ``/api/public/traces/<id>``. *baselines* is the
    per-batch median thresholds — when omitted, relative flags are
    suppressed (used by unit tests that don't construct a baseline).
    *started_at* is the process start time for restart correlation.
    """
    flags: list[str] = []
    total_cost = float(trace.get("totalCost") or 0.0)

    _per_obs_cost = (
        (total_cost / len(observations))
        if observations and len(observations) > 0
        else None
    )
    _is_cheap_high_volume = (
        _per_obs_cost is not None
        and _per_obs_cost < settings.trace_review_per_obs_cost_threshold
    )

    if (
        not _is_cheap_high_volume
        and baselines is not None
        and baselines.cost_threshold is not None
        and total_cost > baselines.cost_threshold
    ):
        flags.append(
            f"cost_outlier (${total_cost:.2f} vs "
            f"${baselines.cost_threshold:.2f} = "
            f"{settings.trace_review_cost_multiplier:.1f}× "
            f"median ${baselines.cost_median:.2f})"
        )

    if observations is None:
        # No detail fetch — check the summary output field for
        # incomplete traces (root span output is null when the agent
        # exited without a final synthesis step).
        output = trace.get("output")
        if output is None or (isinstance(output, str) and not output.strip()):
            flags.append("incomplete_trace")
            # Restart correlation: compare trace end timestamp against
            # process start time within the configured window.
            if started_at is not None:
                trace_end = _extract_trace_end_time(trace, None)
                if trace_end is not None:
                    delta = abs((trace_end - started_at).total_seconds())
                    if (
                        delta
                        <= settings.trace_review_restart_correlation_window_seconds
                    ):
                        flags.append("restart_correlated")
        return _TraceFlags(
            trace_id=trace.get("id", ""),
            trace_name=trace.get("name") or "(unnamed)",
            session_id=trace.get("sessionId") or "",
            total_cost=total_cost,
            flags=flags,
        )

    # Observation-level flags — counted on the deduped list so each tool
    # call's TOOL + SPAN pair is counted once.
    counted = _dedupe_tool_spans(observations)
    if (
        baselines is not None
        and baselines.obs_threshold is not None
        and len(counted) > baselines.obs_threshold
    ):
        flags.append(
            f"observation_storm ({len(counted)} obs vs "
            f"threshold {baselines.obs_threshold:.0f} = "
            f"{settings.trace_review_obs_multiplier:.1f}× "
            f"median {baselines.obs_median:.0f})"
        )

    # Per-tool call count + tool-error scan in a single pass.
    tool_calls: dict[str, int] = {}
    tool_errors = 0
    ask_user_calls = 0
    explore_runs = 0
    for o in counted:
        name = o.get("name") or ""

        # Count tool invocations.
        if name and not name.startswith("chat ") and name != "":
            tool_calls[name] = tool_calls.get(name, 0) + 1

        if name == "explore run":
            explore_runs += 1
        if name == "ask_user":
            ask_user_calls += 1

        # Tool errors — check the tool's output / status for a marker.
        out = o.get("output")
        out_s = (
            out
            if isinstance(out, str)
            else (json.dumps(out, default=str) if out is not None else "")
        )
        status_msg = o.get("statusMessage") or ""
        if name and not name.startswith("chat "):
            # ask_user returns are controlled values (the
            # __ASK_USER_PAUSE__ pause sentinel, or a
            # self-describing diagnostic string) — never a genuine
            # tool failure.  Exclude from the error scan regardless
            # of the recorded output/status text.  NOTE: the
            # sentinel itself does not match _TOOL_ERR_PATTERNS;
            # skipping by tool name guards against any ask_user
            # output (e.g. a controlled "Error: no active ticket"
            # return) tripping the regex.
            if (
                name == "ask_user"
                or (
                    name == "run_command"
                    and (
                        out_s.startswith("exit=0\n")
                        or "Your command ran successfully" in out_s
                    )
                )
                or (name == "read_file" and "does not exist — try" in out_s)
                or (
                    name == "run_command"
                    and "failed with exit code" in out_s
                    and "and produced no output" in out_s
                )
                or (name == "run_command" and "error: could not apply" in out_s)
                or name == "ticket_description"
            ):
                pass
            elif _TOOL_ERR_PATTERNS.search(out_s) or _TOOL_ERR_PATTERNS.search(
                status_msg
            ):
                tool_errors += 1

    if tool_errors:
        flags.append(f"tool_errors ({tool_errors})")
    if explore_runs > 5:
        flags.append(f"explore_storm ({explore_runs} explore runs)")
    if ask_user_calls > 1:
        # Multiple pauses on the same trace is almost always a bug
        # (the agent didn't actually solve the original ambiguity).
        flags.append(f"ask_user_loop ({ask_user_calls} pauses)")
    for tool_name, count in tool_calls.items():
        if count > settings.trace_review_max_repeated_tool:
            flags.append(f"repeated_tool {tool_name} ({count})")
            break  # one is enough to trigger

    # Incomplete trace detection: only GENERATION observations represent
    # LLM model output. SPAN/EVENT observations (root spans, agent spans,
    # tool calls) are parent containers whose endTime spans their children,
    # so they always sort last and must not trigger incomplete_trace. Among
    # the GENERATION observations, the trace is incomplete only when the
    # latest one is not a "chat " completion (the synthesis step). Sort by
    # endTime so ordering is deterministic even when Langfuse reorders.
    gen_obs = [o for o in observations if o.get("type") == "GENERATION"]
    if gen_obs:
        sorted_obs = sorted(
            gen_obs,
            key=lambda o: o.get("endTime") or o.get("startTime") or "",
        )
        last_obs = sorted_obs[-1]
        last_name = last_obs.get("name") or ""
        if last_name and not last_name.startswith("chat "):
            flags.append("incomplete_trace")

    # Restart correlation: when incomplete_trace fires AND the trace's
    # latest timestamp falls within the correlation window of process
    # start, the trace was likely killed by a container restart rather
    # than an agent-loop bug.
    if "incomplete_trace" in flags and started_at is not None:
        trace_end = _extract_trace_end_time(trace, observations)
        if trace_end is not None:
            delta = abs((trace_end - started_at).total_seconds())
            if delta <= settings.trace_review_restart_correlation_window_seconds:
                flags.append("restart_correlated")

    return _TraceFlags(
        trace_id=trace.get("id", ""),
        trace_name=trace.get("name") or "(unnamed)",
        session_id=trace.get("sessionId") or "",
        total_cost=total_cost,
        flags=flags,
    )
