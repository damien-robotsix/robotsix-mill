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
import os
import sys


def _flag(s: str) -> str:
    """ANSI colour helpers — no dependency on rich/colorama."""
    _COLORS = {"GREEN": "\033[92m", "RED": "\033[91m", "RESET": "\033[0m", "BOLD": "\033[1m"}
    return _COLORS.get(s, "")


def inspect(trace_id: str, *, json_out: bool = False) -> int:
    from robotsix_mill.config import Settings, get_secrets
    from robotsix_mill.langfuse_client import fetch_trace_observations, fetch_trace_detail

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
        print(f"FAIL: trace {trace_id} has zero observations — did event-mode export work?")
        return 2

    # ------------------------------------------------------------------
    # Check results accumulator
    # ------------------------------------------------------------------
    checks: list[dict] = []

    def check(name: str, passed: bool, detail_msg: str = "") -> None:
        checks.append({"check": name, "passed": passed, "detail": detail_msg})

    # ------------------------------------------------------------------
    # 0. Count observations by type
    # ------------------------------------------------------------------
    from collections import Counter

    type_counts: Counter[str] = Counter(o.get("type") or "UNKNOWN" for o in observations)
    type_summary = ", ".join(f"{t}={c}" for t, c in sorted(type_counts.items()))
    check("observations_present", len(observations) > 0, f"total={len(observations)}")
    if not json_out:
        print(f"Observations: {len(observations)}  ({type_summary})\n")

    # ------------------------------------------------------------------
    # 1. At least one GENERATION-type observation
    # ------------------------------------------------------------------
    generations = [o for o in observations if o.get("type") == "GENERATION"]
    check(
        "has_generations",
        len(generations) >= 1,
        f"found {len(generations)} GENERATION observation(s)",
    )

    # ------------------------------------------------------------------
    # 2. At least one observation has input containing a user message
    #    and output containing an assistant response.
    # ------------------------------------------------------------------
    found_input_output = False
    for o in observations:
        inp = o.get("input")
        out = o.get("output")
        if inp and out:
            # Heuristic: at least one GENERATION has non-trivial input+output.
            # The input may be JSON (Langfuse renders it), output is text.
            if o.get("type") == "GENERATION":
                found_input_output = True
                break
    check("has_input_output", found_input_output)

    # ------------------------------------------------------------------
    # 3. At least one observation represents a tool call.
    #    In event-mode, pydantic-ai emits tool-call LogRecords; Langfuse
    #    surfaces them as observations with gen_ai.tool.name or similar.
    #    Check both the observation name and nested attributes.
    # ------------------------------------------------------------------
    tool_obs = []
    for o in observations:
        name = (o.get("name") or "").lower()
        inp = o.get("input")
        outp = o.get("output")

        # Direct name-based heuristic — Langfuse often labels tool
        # observations with the tool name.
        if "get_server_status" in name:
            tool_obs.append(o)
            continue

        # Check whether input/output fields contain tool-call markers.
        # pydantic-ai event-mode tool calls carry gen_ai.tool.name.
        if isinstance(inp, dict):
            if inp.get("gen_ai.tool.name") or inp.get("tool_name"):
                tool_obs.append(o)
                continue
        if isinstance(inp, str) and "tool" in inp.lower():
            # Weak heuristic — only accept if output also looks like tool result.
            if isinstance(outp, str) and "server" in outp.lower():
                tool_obs.append(o)
                continue

    check("has_tool_call", len(tool_obs) >= 1, f"found {len(tool_obs)} tool-call observation(s)")

    # ------------------------------------------------------------------
    # 4. No truncation or error statusMessage on any observation
    # ------------------------------------------------------------------
    truncation_errors = []
    for o in observations:
        msg = o.get("statusMessage") or ""
        if msg:
            truncation_errors.append(f"[{o.get('type')}] {msg[:120]}")
    check(
        "no_truncation_or_error",
        len(truncation_errors) == 0,
        "; ".join(truncation_errors) if truncation_errors else "all clean",
    )

    # ------------------------------------------------------------------
    # 5. Model & usage present on GENERATION observations
    # ------------------------------------------------------------------
    gen_with_model = 0
    gen_with_usage = 0
    gen_total = 0
    for o in generations:
        gen_total += 1
        if o.get("model"):
            gen_with_model += 1
        usage = o.get("usage") or {}
        if usage.get("input") is not None or usage.get("output") is not None:
            gen_with_usage += 1

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

    # ------------------------------------------------------------------
    # 6. Total cost is present and non-zero on the trace
    # ------------------------------------------------------------------
    total_cost = None
    if detail:
        total_cost = detail.get("totalCost")
    cost_ok = total_cost is not None and float(total_cost) > 0
    check(
        "trace_cost_nonzero",
        cost_ok,
        f"totalCost={total_cost}",
    )

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------
    if json_out:
        result = {
            "trace_id": trace_id,
            "observation_count": len(observations),
            "type_counts": dict(type_counts),
            "checks": checks,
            "total_cost": total_cost,
        }
        print(json.dumps(result, indent=2, default=str))
    else:
        passed = sum(1 for c in checks if c["passed"])
        failed = sum(1 for c in checks if not c["passed"])
        for c in checks:
            icon = f"{_flag('GREEN')}✓{_flag('RESET')}" if c["passed"] else f"{_flag('RED')}✗{_flag('RESET')}"
            detail = f" — {c['detail']}" if c["detail"] else ""
            print(f"  {icon} {c['check']}{detail}")
        print(f"\n{passed}/{len(checks)} checks passed, {failed} failed")

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
