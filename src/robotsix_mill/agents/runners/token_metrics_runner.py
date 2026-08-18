"""Periodic token-metrics aggregation.

Daily, deterministic, no-LLM pass.  Queries the shared Langfuse project
for the traces created in the last ``window_seconds``, extracts per-step
``mill.step_usage`` metadata (stage × model token counts) directly from
the list endpoint — never the full trace detail, so no prompt payloads
are fetched — and writes a compact JSON snapshot to
``<data_dir>/token_metrics/<YYYY-MM-DD>.json``.

Each snapshot holds per-call input/output-token percentiles (p50/p95/max,
plus count/sum/mean/min) grouped by stage × model, which is what the
context-reduction targets (review −50%, ci_fix −40%, implement reduction)
need to be verified without paging through 200 KB observation payloads.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ...config import Settings
from ...langfuse.client import list_all_traces_since, trace_step_usage_records

log = logging.getLogger("robotsix_mill.token_metrics")


def _percentile(sorted_values: list[int], p: float) -> float:
    """Nearest-rank percentile of *sorted_values* (honest, no interpolation).

    ``sorted_values`` must already be sorted ascending and non-empty.
    """
    if not sorted_values:
        return 0.0
    idx = max(0, math.ceil(p / 100.0 * len(sorted_values)) - 1)
    return float(sorted_values[idx])


def _token_statistics(values: list[int]) -> dict[str, float]:
    """Compact distribution summary for a list of per-call token counts."""
    if not values:
        return {
            "count": 0,
            "sum": 0.0,
            "mean": 0.0,
            "min": 0.0,
            "p50": 0.0,
            "p95": 0.0,
            "max": 0.0,
        }
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "sum": float(sum(ordered)),
        "mean": round(sum(ordered) / len(ordered), 2),
        "min": float(ordered[0]),
        "p50": _percentile(ordered, 50.0),
        "p95": _percentile(ordered, 95.0),
        "max": float(ordered[-1]),
    }


def aggregate_step_usage(traces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate per-step usage from *traces* into stage × model buckets.

    Pure function (no I/O) so it can be unit-tested directly: one bucket
    per ``(stage_name, model_name)`` pair, each holding the per-call
    ``input_tokens`` / ``output_tokens`` distributions.
    """
    buckets: dict[tuple[str, str], dict[str, list[int]]] = {}
    for trace in traces:
        for su in trace_step_usage_records(trace):
            stage = str(su.get("stage_name") or "unknown")
            model = str(su.get("model_name") or "unknown")
            key = (stage, model)
            entry = buckets.setdefault(key, {"input": [], "output": []})
            try:
                entry["input"].append(int(su.get("input_tokens") or 0))
            except TypeError, ValueError:
                entry["input"].append(0)
            try:
                entry["output"].append(int(su.get("output_tokens") or 0))
            except TypeError, ValueError:
                entry["output"].append(0)

    out: list[dict[str, Any]] = []
    for (stage, model), entry in sorted(buckets.items()):
        out.append(
            {
                "stage": stage,
                "model": model,
                "calls": len(entry["input"]),
                "input_tokens": _token_statistics(entry["input"]),
                "output_tokens": _token_statistics(entry["output"]),
            }
        )
    return out


@dataclass
class TokenMetricsResult:
    """Result of one token-metrics aggregation pass."""

    window_start: str  # ISO 8601
    window_end: str  # ISO 8601
    traces_seen: int
    steps_seen: int
    buckets: list[dict[str, Any]] = field(default_factory=list)
    path: Path | None = None
    written: bool = False

    @property
    def summary(self) -> str:
        """One-line summary of the aggregation pass."""
        dest = self.path if self.written else "(not written)"
        return (
            f"steps={self.steps_seen} traces={self.traces_seen} "
            f"buckets={len(self.buckets)} -> {dest}"
        )


def run_token_metrics_aggregation(
    *,
    settings: Settings,
    window_seconds: int = 86400,
) -> TokenMetricsResult:
    """Run one aggregation sweep against the shared Langfuse project.

    Returns a :class:`TokenMetricsResult`.  Never raises — Langfuse
    outages and write failures are logged and the worker retries on the
    next interval.
    """
    now = datetime.now(UTC)
    window_start = now - timedelta(seconds=max(60, window_seconds))
    window_start_iso = window_start.isoformat()
    window_end_iso = now.isoformat()

    # Mirror _build_read_client(settings, repo_config=None): an unconfigured
    # Langfuse (no block, or block with no projects) leaves tracing disabled,
    # so short-circuit before any HTTP or file I/O.
    if not settings.tracing_enabled:
        log.debug("token-metrics: Langfuse not configured — skipping")
        return TokenMetricsResult(
            window_start=window_start_iso,
            window_end=window_end_iso,
            traces_seen=0,
            steps_seen=0,
        )

    try:
        traces = list_all_traces_since(settings, window_start_iso, repo_config=None)
    except Exception:
        log.exception("token-metrics: failed to list traces")
        return TokenMetricsResult(
            window_start=window_start_iso,
            window_end=window_end_iso,
            traces_seen=0,
            steps_seen=0,
        )

    buckets = aggregate_step_usage(traces)
    steps_seen = sum(int(b["calls"]) for b in buckets)

    out_dir = settings.data_dir / "token_metrics"
    snapshot_path = out_dir / f"{now.date().isoformat()}.json"
    snapshot = {
        "window_start": window_start_iso,
        "window_end": window_end_iso,
        "generated_at": window_end_iso,
        "traces_seen": len(traces),
        "steps_seen": steps_seen,
        "buckets": buckets,
    }

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(
            json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError:
        log.exception("token-metrics: failed to write %s", snapshot_path)
        return TokenMetricsResult(
            window_start=window_start_iso,
            window_end=window_end_iso,
            traces_seen=len(traces),
            steps_seen=steps_seen,
            buckets=buckets,
        )

    return TokenMetricsResult(
        window_start=window_start_iso,
        window_end=window_end_iso,
        traces_seen=len(traces),
        steps_seen=steps_seen,
        buckets=buckets,
        path=snapshot_path,
        written=True,
    )
