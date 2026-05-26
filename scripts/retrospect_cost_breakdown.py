"""Per-trace cost + token breakdown for retrospect sessions.

Surfaces the actual cost drivers behind retrospect runs by drilling into
Langfuse: every retrospect session's traces, the model used per
generation, input/output tokens, observation count, and aggregated
totals. Especially useful when investigating deep-analysis runs (the
~10× outliers that include `trace_inspect` + `cross_trace_analyze`
sub-agents).

Run from the host's local venv (the container doesn't bind-mount
scripts/, but the host has the same robotsix_mill package installed):

    .venv/bin/python3 scripts/retrospect_cost_breakdown.py --hours 72
    .venv/bin/python3 scripts/retrospect_cost_breakdown.py --top 5
    .venv/bin/python3 scripts/retrospect_cost_breakdown.py --session-id <sid>

The trace name `retrospect` is matched; `trace_inspect` and
`cross_trace_analyze` sub-agent traces share the same Langfuse
session_id (the parent retrospect's) so they roll up automatically.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from robotsix_mill.config import get_repos_config, load_settings
from robotsix_mill.langfuse_client import _langfuse_api_get


def _fetch_traces(
    settings, hours: float, repo_config=None
) -> list[dict[str, Any]]:
    """Pull retrospect traces from Langfuse over the last `hours`."""
    from_ts = (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).isoformat()
    all_traces: list[dict[str, Any]] = []
    page = 1
    while True:
        r = _langfuse_api_get(
            settings,
            "/api/public/traces",
            params={
                "name": "retrospect",
                "fromTimestamp": from_ts,
                "limit": 100,
                "page": page,
            },
            repo_config=repo_config,
        )
        data = r.get("data", []) or []
        if not data:
            break
        all_traces.extend(data)
        if len(data) < 100:
            break
        page += 1
    return all_traces


def _fetch_session_traces(
    settings, session_id: str, repo_config=None
) -> list[dict[str, Any]]:
    """Pull all traces (any name) for a single session — picks up
    `retrospect` plus `trace_inspect` / `cross_trace_analyze`
    sub-calls that share the session_id."""
    r = _langfuse_api_get(
        settings,
        "/api/public/traces",
        params={"sessionId": session_id, "limit": 100},
        repo_config=repo_config,
    )
    return r.get("data", []) or []


def _fetch_observations(
    settings, trace_id: str, repo_config=None
) -> list[dict[str, Any]]:
    """Pull the observation tree for one trace.  Generations carry the
    per-call model name + token usage."""
    r = _langfuse_api_get(
        settings, f"/api/public/traces/{trace_id}", repo_config=repo_config,
    )
    return r.get("observations", []) or []


def _gen_summary(obs: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate generations in the observation tree by model.

    Note on cost: Langfuse's per-observation ``totalCost`` is unreliable
    in our setup (typically 0); only the trace-level ``totalCost`` is
    populated. We track per-model token counts here and let the caller
    derive a rough per-model dollar share by ratio against the
    trace-level total if needed.
    """
    by_model: dict[str, dict[str, int]] = defaultdict(
        lambda: {"calls": 0, "in_tok": 0, "out_tok": 0}
    )
    n_total = len(obs)
    n_generations = 0
    for o in obs:
        if o.get("type") != "GENERATION":
            continue
        n_generations += 1
        model = o.get("model") or "?"
        usage = o.get("usageDetails") or o.get("usage") or {}
        in_tok = (
            usage.get("input") if isinstance(usage.get("input"), (int, float))
            else usage.get("promptTokens", 0)
        )
        out_tok = (
            usage.get("output") if isinstance(usage.get("output"), (int, float))
            else usage.get("completionTokens", 0)
        )
        by_model[model]["calls"] += 1
        by_model[model]["in_tok"] += int(in_tok or 0)
        by_model[model]["out_tok"] += int(out_tok or 0)
    return {
        "n_observations": n_total,
        "n_generations": n_generations,
        "by_model": dict(by_model),
    }


def _print_session(
    settings, parent: dict[str, Any], repo_config=None, depth: int = 0
) -> tuple[float, int, int, int]:
    """Print one retrospect session's full breakdown (parent +
    same-session sub-agent traces). Returns (cost, in_tok, out_tok,
    generations) for grand-total aggregation.

    Output format — one block per session:

        ───────────────────────────────────────────────────────────────
        SESSION  <session_id>
        ───────────────────────────────────────────────────────────────
        retrospect           cost=$0.0850  in=  85234  out= 12100  gens=  3
          deepseek/deepseek-v4-pro    calls= 3  in= 85234  out=12100  cost=$0.0850
        trace_inspect        cost=$0.0095  in=   8200  out=   620  gens=  1
          deepseek/deepseek-v4-flash  calls= 1  in=  8200  out=  620  cost=$0.0095
        ... (one row per sub-trace)
        TOTAL                cost=$0.1248  in=N tokens  out=N tokens  gens=K
    """
    sid = parent.get("sessionId") or parent.get("id")
    print()
    print("─" * 75)
    print(f"SESSION  {sid}")
    print(f"  start  {parent.get('timestamp','')[:19]}   "
          f"parent cost ${parent.get('totalCost', 0):.4f}")
    print("─" * 75)

    session_traces = _fetch_session_traces(settings, sid, repo_config=repo_config)
    # Ensure parent retrospect prints first; sub-traces after by timestamp.
    session_traces.sort(
        key=lambda t: (t.get("name") != "retrospect", t.get("timestamp", ""))
    )
    total_cost = 0.0
    total_in = 0
    total_out = 0
    total_gens = 0
    for t in session_traces:
        name = t.get("name", "?")
        tcost = t.get("totalCost") or 0.0
        obs = _fetch_observations(settings, t["id"], repo_config=repo_config)
        s = _gen_summary(obs)
        in_tok = sum(m["in_tok"] for m in s["by_model"].values())
        out_tok = sum(m["out_tok"] for m in s["by_model"].values())
        total_cost += float(tcost)
        total_in += in_tok
        total_out += out_tok
        total_gens += s["n_generations"]
        print(
            f"  {name:22s} cost=${tcost:7.4f}  in={in_tok:7d}  "
            f"out={out_tok:6d}  gens={s['n_generations']:3d}  "
            f"obs={s['n_observations']:4d}"
        )
        # Rank models by input token volume (the dominant cost driver
        # for retrospect — the prompt is huge, the response is small).
        for model, stats in sorted(
            s["by_model"].items(), key=lambda kv: -kv[1]["in_tok"]
        ):
            # Derive a dollar share from the trace-level totalCost
            # weighted by this model's input-token share. Per-observation
            # cost from Langfuse is empty here, so this is the best
            # approximation we have. Marked with ~ to flag it as derived.
            tok_share = stats["in_tok"] / max(in_tok, 1) if in_tok else 0
            est_cost = float(tcost) * tok_share
            print(
                f"      {model:36s} calls={stats['calls']:3d}  "
                f"in={stats['in_tok']:7d}  out={stats['out_tok']:6d}  "
                f"~cost=${est_cost:7.4f}"
            )
    print(
        f"  {'SESSION TOTAL':22s} cost=${total_cost:7.4f}  "
        f"in={total_in:7d}  out={total_out:6d}  gens={total_gens:3d}"
    )
    return total_cost, total_in, total_out, total_gens


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--hours", type=float, default=72,
        help="Look back this many hours (default: 72).",
    )
    p.add_argument(
        "--top", type=int, default=10,
        help="Show only the N most expensive sessions in the window "
             "(default: 10). Use --all to bypass the cap.",
    )
    p.add_argument(
        "--all", action="store_true",
        help="Show every session in the window (no top-N cap).",
    )
    p.add_argument(
        "--session-id", type=str, default=None,
        help="Drill into one specific Langfuse session_id "
             "(skips the window scan).",
    )
    p.add_argument(
        "--repo-id", type=str, default=None,
        help="Repo to query Langfuse for (default: first repo in "
             "config/repos.yaml).",
    )
    args = p.parse_args()

    settings = load_settings()

    # Resolve which repo's Langfuse project to query.
    repo_config = None
    try:
        repos = get_repos_config()
        if args.repo_id:
            repo_config = repos.repos.get(args.repo_id)
            if repo_config is None:
                sys.stderr.write(
                    f"unknown repo: {args.repo_id}; known: "
                    f"{sorted(repos.repos.keys())}\n"
                )
                return 2
        elif repos.repos:
            # Default to first configured repo.
            repo_config = next(iter(repos.repos.values()))
    except Exception as e:
        sys.stderr.write(f"warning: could not load repos config: {e}\n")

    if args.session_id:
        # Synthesize a parent-like dict to feed _print_session.
        fake_parent = {"sessionId": args.session_id, "timestamp": "", "id": args.session_id}
        cost, in_tok, out_tok, gens = _print_session(
            settings, fake_parent, repo_config=repo_config
        )
        print()
        print(f"GRAND TOTAL  cost=${cost:.4f}  in={in_tok}  out={out_tok}  gens={gens}")
        return 0

    print(
        f"Scanning retrospect traces in the last {args.hours}h "
        f"(repo: {repo_config.repo_id if repo_config else '(global settings)'})..."
    )
    traces = _fetch_traces(
        settings, hours=args.hours, repo_config=repo_config
    )
    if not traces:
        print("No retrospect traces found in the window.")
        return 0

    # Rank parents by their reported totalCost (the API value reflects
    # sub-call costs that share the session_id, so this is the right
    # sort key for "expensive sessions").
    traces.sort(key=lambda t: -(t.get("totalCost") or 0))
    n_total = len(traces)
    cap = None if args.all else max(1, args.top)
    if cap:
        traces = traces[:cap]
    print(
        f"Found {n_total} retrospect session(s); showing "
        f"{'all' if not cap else f'top {cap}'} by cost.\n"
    )

    grand_cost = 0.0
    grand_in = 0
    grand_out = 0
    grand_gens = 0
    for t in traces:
        c, i, o, g = _print_session(settings, t, repo_config=repo_config)
        grand_cost += c
        grand_in += i
        grand_out += o
        grand_gens += g

    print()
    print("═" * 75)
    print(
        f"GRAND TOTAL across {len(traces)} session(s)   "
        f"cost=${grand_cost:.4f}  in_tok={grand_in}  out_tok={grand_out}  "
        f"generations={grand_gens}"
    )
    if grand_gens:
        print(
            f"Average per generation: ${grand_cost / grand_gens:.4f}  "
            f"({grand_in // grand_gens} in / "
            f"{grand_out // grand_gens} out tokens)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
