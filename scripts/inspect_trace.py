"""Programmatic inspection of a Langfuse trace produced by event-mode
logging.  Checks the observation tree against the acceptance criteria
for the switch-pydantic-ai-instrumentation-to-event-mode epic.

Usage:
    python scripts/inspect_trace.py <TRACE_ID>
    python scripts/inspect_trace.py <TRACE_ID> --json   # machine-readable

Requires the same Langfuse credentials as the mill runtime:
    LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST

Runs a set of heuristics against the observation list and reports
pass / fail for each check.  This is an operator helper, not an
automated CI test — a pass here is *suggestive* but the operator
still does the visual UI check.
"""

from __future__ import annotations

import json
import sys
from collections import Counter


def _flag(s: str) -> str:
    """ANSI colour helpers — no dependency on rich/colorama."""
    _COLORS = {
        "GREEN": "\033[92m",
        "RED": "\033[91m",
        "RESET": "\033[0m",
        "BOLD": "\033[1m",
    }
    return _COLORS.get(s, "")


def _has_input_output_generation(observations: list[dict]) -> bool:
    """Return True if any GENERATION observation has non-empty input AND output."""
    for o in observations:
        if o.get("type") == "GENERATION" and o.get("input") and o.get("output"):
            return True
    return False


def _is_tool_observation(o: dict) -> bool:
    """Heuristic: does this observation look like a tool call?"""
    name = (o.get("name") or "").lower()
    if "get_server_status" in name:
        return True

    inp = o.get("input")
    outp = o.get("output")

    # pydantic-ai event-mode tool calls carry gen_ai.tool.name.
    if isinstance(inp, dict) and (inp.get("gen_ai.tool.name") or inp.get("tool_name")):
        return True

    # Weak heuristic — only accept if input mentions tool AND output looks like a tool result.
    if (
        isinstance(inp, str)
        and "tool" in inp.lower()
        and isinstance(outp, str)
        and "server" in outp.lower()
    ):
        return True

    return False


def _collect_tool_observations(observations: list[dict]) -> list[dict]:
    return [o for o in observations if _is_tool_observation(o)]


def _collect_truncation_errors(observations: list[dict]) -> list[str]:
    errors: list[str] = []
    for o in observations:
        msg = o.get("statusMessage") or ""
        if msg:
            errors.append(f"[{o.get('type')}] {msg[:120]}")
    return errors


def _count_generation_meta(generations: list[dict]) -> tuple[int, int]:
    """Return (count_with_model, count_with_usage) across the generations."""
    with_model = 0
    with_usage = 0
    for o in generations:
        if o.get("model"):
            with_model += 1
        usage = o.get("usage") or {}
        if usage.get("input") is not None or usage.get("output") is not None:
            with_usage += 1
    return with_model, with_usage


def _render_text_report(
    checks: list[dict],
    observations_count: int,
    type_summary: str,
) -> None:
    print(f"Observations: {observations_count}  ({type_summary})\n")
    passed = sum(1 for c in checks if c["passed"])
    failed = sum(1 for c in checks if not c["passed"])
    for c in checks:
        icon = (
            f"{_flag('GREEN')}✓{_flag('RESET')}"
            if c["passed"]
            else f"{_flag('RED')}✗{_flag('RESET')}"
        )
        detail = f" — {c['detail']}" if c["detail"] else ""
        print(f"  {icon} {c['check']}{detail}")
    print(f"\n{passed}/{len(checks)} checks passed, {failed} failed")


def _build_checks(
    observations: list[dict],
    detail: dict | None,
) -> list[dict]:
    """Run all heuristic checks and return a list of result dicts."""
    checks: list[dict] = []

    def check(name: str, passed: bool, detail_msg: str = "") -> None:
        checks.append({"check": name, "passed": passed, "detail": detail_msg})

    # 0. Observations present
    check("observations_present", len(observations) > 0, f"total={len(observations)}")

    # 1. At least one GENERATION-type observation
    generations = [o for o in observations if o.get("type") == "GENERATION"]
    check(
        "has_generations",
        len(generations) >= 1,
        f"found {len(generations)} GENERATION observation(s)",
    )

    # 2. At least one GENERATION observation with input AND output
    check("has_input_output", _has_input_output_generation(observations))

    # 3. At least one observation represents a tool call
    tool_obs = _collect_tool_observations(observations)
    check(
        "has_tool_call",
        len(tool_obs) >= 1,
        f"found {len(tool_obs)} tool-call observation(s)",
    )

    # 4. No truncation or error statusMessage on any observation
    truncation_errors = _collect_truncation_errors(observations)
    check(
        "no_truncation_or_error",
        len(truncation_errors) == 0,
        "; ".join(truncation_errors) if truncation_errors else "all clean",
    )

    # 5. Model & usage present on GENERATION observations
    gen_total = len(generations)
    gen_with_model, gen_with_usage = _count_generation_meta(generations)
    check(
        "model_on_generations",
        gen_with_model == gen_total if gen_total > 0 else True,
        f"{gen_with_model}/{gen_total} generations have model",
    )
    check(
        "usage_on_generations",
        gen_with_usage == gen_total if gen_total > 0 else True,
        f"{gen_with_usage}/{gen_total} generations have usage",
    )

    # 6. Total cost is present and non-zero on the trace
    total_cost = detail.get("totalCost") if detail else None
    cost_ok = total_cost is not None and float(total_cost) > 0
    check("trace_cost_nonzero", cost_ok, f"totalCost={total_cost}")

    return checks


def inspect(trace_id: str, *, json_out: bool = False) -> int:
    from robotsix_mill.config import Settings, get_secrets
    from robotsix_mill.langfuse_client import (
        fetch_trace_observations,
        fetch_trace_detail,
    )

    secrets = get_secrets()
    if not secrets.langfuse_public_key:
        print("FAIL: LANGFUSE_PUBLIC_KEY not configured")
        return 2

    settings = Settings()
    observations = fetch_trace_observations(settings, trace_id)
    detail = fetch_trace_detail(settings, trace_id)

    if observations is None:
        print(f"FAIL: could not fetch trace {trace_id} — is Langfuse reachable?")
        return 2
    if not observations:
        print(
            f"FAIL: trace {trace_id} has zero observations — did event-mode export work?"
        )
        return 2

    type_counts: Counter[str] = Counter(
        o.get("type") or "UNKNOWN" for o in observations
    )
    type_summary = ", ".join(f"{t}={c}" for t, c in sorted(type_counts.items()))

    checks = _build_checks(observations, detail)

    if json_out:
        total_cost = detail.get("totalCost") if detail else None
        result = {
            "trace_id": trace_id,
            "observation_count": len(observations),
            "type_counts": dict(type_counts),
            "checks": checks,
            "total_cost": total_cost,
        }
        print(json.dumps(result, indent=2, default=str))
    else:
        _render_text_report(checks, len(observations), type_summary)

    return 0 if all(c["passed"] for c in checks) else 1


def main() -> int:
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <TRACE_ID> [--json]")
        return 2

    trace_id = sys.argv[1]
    json_out = "--json" in sys.argv
    return inspect(trace_id, json_out=json_out)


if __name__ == "__main__":
    sys.exit(main())
