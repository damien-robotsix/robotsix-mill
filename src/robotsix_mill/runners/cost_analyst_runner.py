"""Cost-analyst runner — global, cross-repo cost-reduction pass.

Two-phase (mirrors trace_review's deterministic→LLM split, but aggregate
instead of per-trace):

    Phase 1 (deterministic, no LLM):
        Walk every registered repo's Langfuse project, fetch traces in the
        window, and build a COST DIGEST with two parts:
          - <aggregate-cost-by-stage>: stages ranked by total spend ACROSS
            ALL REPOS, with %-of-fleet, trace count, avg $/trace, and the
            current model tier each stage runs on.
          - <significant-specimens>: four fully-expanded worked examples —
            the most expensive trace, the most expensive ticket, the trace
            with the most errors, and the ticket with the most steps.

    Phase 2 (LLM):
        Run the cost_analyst agent (default tier) over the digest. Each
        returned proposal becomes a high-confidence cost-reduction draft on
        the mill board (deduplicated by gap-id against open
        ``source=cost-analyst`` tickets).

Seam: tests monkeypatch ``run_cost_analyst_agent`` and the
``robotsix_mill.langfuse.client`` fetchers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..agents.cost_analyst import (
    MAX_PROPOSALS,
    CostReductionResult,
    run_cost_analyst_agent,
)
from ..config import RepoConfig, Settings, get_repos_config
from ..core.models import SourceKind
from ..core.service import TicketService
from ..langfuse import client as lf
from ..runners.pass_runner import load_memory, persist_memory

log = logging.getLogger("robotsix_mill.cost_analyst")


# ---------------------------------------------------------------------------
# Stage → current-model-tier resolution (best-effort, for the digest)
# ---------------------------------------------------------------------------

# Maps a pipeline-stage / agent name to the Settings attribute that holds the
# model it runs on. Stages not listed get "(model varies — see specimens)";
# the four specimens always carry the real per-observation models.
_STAGE_MODEL_SETTING: dict[str, str] = {
    "implement": "model",
    "refine": "refine_model",
    "retrospect": "retrospect_model",
    "answer": "answer_model",
    "audit": "audit_model",
    "trace_review": "trace_review_model",
    "trace_inspector": "trace_inspector_model",
    "no_change": "no_change_model",
    "explore": "explore_model",
    "test": "test_model",
    "data_dir_audit": "data_dir_audit_model",
}


def _tier_label(model_name: str) -> str:
    """Classify a model string into a coarse tier label for the digest."""
    if not model_name:
        return "?"
    return "cheap" if "flash" in model_name.lower() else "capable/default"


def _stage_tier(settings: Settings, stage: str) -> str:
    attr = _STAGE_MODEL_SETTING.get(stage)
    if attr is None:
        return "(model varies — see specimens)"
    model = str(getattr(settings, attr, "") or "")
    if not model:
        return "(model varies — see specimens)"
    return f"{model} [{_tier_label(model)}]"


# ---------------------------------------------------------------------------
# Phase 1 — deterministic cross-repo cost digest
# ---------------------------------------------------------------------------


@dataclass
class _Collected:
    stage_costs: dict[str, list[float]] = field(default_factory=dict)
    all_named: list[tuple[dict, RepoConfig]] = field(default_factory=list)
    sessions: dict[str, dict] = field(default_factory=dict)


def _repo_has_tracing(repo: RepoConfig) -> bool:
    return bool(
        getattr(repo, "langfuse_public_key", "")
        and getattr(repo, "langfuse_secret_key", "")
    )


def _collect_traces(settings: Settings) -> _Collected:
    """Fetch traces from EVERY registered repo's Langfuse project (one
    window fetch per repo) and bucket them by stage name and by session."""
    window = float(settings.cost_analyst_window_hours)
    out = _Collected()
    for repo in get_repos_config().repos.values():
        if not _repo_has_tracing(repo):
            continue
        traces = lf._fetch_traces_time_window(
            settings,
            window,
            max_pages=10,
            caller_name="cost_analyst",
            repo_config=repo,
        )
        if not traces:
            continue
        for t in traces:
            cost = float(t.get("totalCost") or 0)
            name = t.get("name")
            if isinstance(name, str) and name.strip():
                out.stage_costs.setdefault(name, []).append(cost)
                out.all_named.append((t, repo))
            sid = (t.get("sessionId") or "").strip()
            if sid:
                s = out.sessions.setdefault(
                    sid, {"cost": 0.0, "count": 0, "repo": repo}
                )
                s["cost"] += cost
                s["count"] += 1
    return out


def _render_stage_table(stage_costs: dict[str, list[float]], settings: Settings) -> str:
    total = sum(sum(v) for v in stage_costs.values())
    rows = []
    for name, costs in stage_costs.items():
        s = sum(costs)
        n = len(costs)
        rows.append((name, s, n))
    rows.sort(key=lambda r: r[1], reverse=True)
    lines = [f"Fleet total over the window: ${total:.4f} across {len(rows)} stages.\n"]
    lines.append("stage | total $ | % | traces | avg $/trace | current tier")
    lines.append("--- | --- | --- | --- | --- | ---")
    for name, s, n in rows[: settings.cost_analyst_top_stages + 6]:
        pct = (100 * s / total) if total else 0.0
        avg = (s / n) if n else 0.0
        lines.append(
            f"{name} | ${s:.4f} | {pct:.1f}% | {n} | ${avg:.4f} | "
            f"{_stage_tier(settings, name)}"
        )
    return "\n".join(lines)


def _token_split(obs: list[dict]) -> str:
    """Sum input vs output tokens across GENERATION observations (handles
    the common Langfuse usage key spellings)."""
    inp = out = 0
    for o in obs:
        usage = o.get("usage") or {}
        if not isinstance(usage, dict):
            continue
        inp += int(
            usage.get("input")
            or usage.get("inputTokens")
            or usage.get("promptTokens")
            or 0
        )
        out += int(
            usage.get("output")
            or usage.get("outputTokens")
            or usage.get("completionTokens")
            or 0
        )
    total = inp + out
    if total == 0:
        return "tokens: (usage not reported)"
    return f"tokens: input={inp:,} output={out:,} (input is {100 * inp // total}% of total)"


def _tool_calls(obs: list[dict]) -> str:
    counts: dict[str, int] = {}
    for o in obs:
        if (o.get("type") or "").upper() in ("SPAN", "TOOL", "EVENT"):
            nm = o.get("name") or "?"
            counts[nm] = counts.get(nm, 0) + 1
    if not counts:
        return "tool-calls: (none recorded)"
    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:8]
    return "tool-calls: " + ", ".join(f"{k}×{v}" for k, v in top)


def _models_used(obs: list[dict]) -> str:
    models = sorted({(o.get("model") or "") for o in obs if o.get("model")})
    return "models: " + (", ".join(models) if models else "(none)")


def _trace_specimen_block(
    settings: Settings, label: str, trace: dict, repo: RepoConfig, extra: str = ""
) -> str:
    obs = lf.fetch_trace_observations(settings, trace.get("id") or "", repo) or []
    errs = sum(1 for o in obs if lf._observation_is_error(o))
    return (
        f"### {label}\n"
        f"- stage: `{trace.get('name', '?')}`  repo: `{repo.repo_id}`  "
        f"cost: ${float(trace.get('totalCost') or 0):.4f}{extra}\n"
        f"- {_models_used(obs)}\n"
        f"- {_token_split(obs)}\n"
        f"- {_tool_calls(obs)}\n"
        f"- observations: {len(obs)} ({errs} error/warning)\n"
    )


def _session_specimen_block(
    settings: Settings, label: str, sid: str, info: dict, extra: str = ""
) -> str:
    repo: RepoConfig = info["repo"]
    steps = lf.session_traces(settings, sid, repo_config=repo) or []
    seq = " → ".join(
        f"{s.get('name', '?')}(${float(s.get('cost') or 0):.3f})" for s in steps[:25]
    )
    return (
        f"### {label}\n"
        f"- session: `{sid}`  repo: `{repo.repo_id}`  "
        f"total: ${info['cost']:.4f}  steps(traces): {info['count']}{extra}\n"
        f"- stage sequence: {seq or '(no per-step data)'}\n"
    )


def _build_specimens_block(settings: Settings, col: _Collected) -> str:
    blocks: list[str] = []

    # 1. Most expensive trace
    if col.all_named:
        t, repo = max(col.all_named, key=lambda tr: float(tr[0].get("totalCost") or 0))
        blocks.append(_trace_specimen_block(settings, "Most expensive trace", t, repo))

    # 2. Most expensive ticket (session)
    if col.sessions:
        sid, info = max(col.sessions.items(), key=lambda kv: kv[1]["cost"])
        blocks.append(
            _session_specimen_block(settings, "Most expensive ticket", sid, info)
        )

    # 3. Trace with the most errors — scan the costliest candidates only.
    candidates = sorted(
        col.all_named, key=lambda tr: float(tr[0].get("totalCost") or 0), reverse=True
    )[:40]
    best_err: tuple[dict, RepoConfig, int] | None = None
    for t, repo in candidates:
        obs = lf.fetch_trace_observations(settings, t.get("id") or "", repo)
        if not obs:
            continue
        n = sum(1 for o in obs if lf._observation_is_error(o))
        if best_err is None or n > best_err[2]:
            best_err = (t, repo, n)
    if best_err and best_err[2] > 0:
        blocks.append(
            _trace_specimen_block(
                settings,
                "Trace with the most errors",
                best_err[0],
                best_err[1],
                extra=f"  errors: {best_err[2]}",
            )
        )

    # 4. Ticket with the most steps
    if col.sessions:
        sid, info = max(col.sessions.items(), key=lambda kv: kv[1]["count"])
        blocks.append(
            _session_specimen_block(settings, "Ticket with the most steps", sid, info)
        )

    return "\n".join(blocks) if blocks else "(no specimens — no traces in window)"


def _build_cost_digest(settings: Settings) -> str:
    """Build the full cost digest (both sections) as prompt text."""
    from ..agents.prompt_blocks import section

    col = _collect_traces(settings)
    if not col.all_named:
        return section(
            "aggregate-cost-by-stage",
            "(no traces found across any registered repo in the window)",
        )
    digest = section(
        "aggregate-cost-by-stage", _render_stage_table(col.stage_costs, settings)
    )
    digest += section("significant-specimens", _build_specimens_block(settings, col))
    return digest


# ---------------------------------------------------------------------------
# Phase 2 — file drafts (gap-id dedup against open cost-analyst tickets)
# ---------------------------------------------------------------------------


def _existing_title_keys(service: TicketService) -> set[str]:
    """Normalized titles of recent cost-analyst tickets — a backstop against
    re-filing the same proposal the agent already has in recent-proposals."""
    from ..dedup import normalize

    return {
        normalize(t.title)[:60]
        for t in service.recent_proposals_for(SourceKind.COST_ANALYST, limit=200)
    }


def _file_drafts(
    result: CostReductionResult,
    settings: Settings,
    session_id: str,
    board_id: str,
) -> list[dict]:
    from ..dedup import normalize

    service = TicketService(settings, board_id=board_id)
    seen = _existing_title_keys(service)
    created: list[dict] = []
    triples = list(zip(result.draft_titles, result.draft_bodies, result.gap_ids))
    for title, body, gap_id in triples[:MAX_PROPOSALS]:
        key = normalize(title)[:60]
        if key in seen:
            log.info("cost_analyst: skipping duplicate proposal %r", title)
            continue
        seen.add(key)
        full_body = f"{body}\n\n<!-- cost-analyst-gap-id: {gap_id} -->"
        try:
            ticket = service.create(
                title=title,
                description=full_body,
                source=SourceKind.COST_ANALYST,
                origin_session=session_id,
            )
            created.append({"id": ticket.id, "title": ticket.title})
            log.info("cost_analyst: filed cost-reduction draft %r", ticket.id)
        except Exception:
            log.exception("cost_analyst: failed to create draft %r", title)
    return created


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


@dataclass
class CostAnalystPassResult:
    updated_memory: str
    drafts_created: list[dict]
    session_id: str


def _gather_recent_proposals(settings: Settings, board_id: str) -> str:
    from ..runners.pass_runner import _format_recent_proposals

    service = TicketService(settings, board_id=board_id)
    tickets = service.recent_proposals_for(SourceKind.COST_ANALYST, limit=100)
    return _format_recent_proposals(tickets)


def run_cost_analyst_pass(session_id: str) -> CostAnalystPassResult:
    """Run a full cost-analyst pass end-to-end.

    1. Build the cross-repo cost digest (deterministic).
    2. Load the cost-analyst memory ledger + gather prior proposals.
    3. Run the cost-analyst agent over the digest.
    4. File high-confidence drafts to the target (mill) board.
    5. Persist updated memory.
    """
    settings = Settings()
    board_id = settings.cost_analyst_target_repo_id

    # 1. Digest (force tracing of THIS pass to the mill project, like meta).
    mill_repo = get_repos_config().repos.get(board_id)
    tracer_ctx = None
    if mill_repo is not None:
        from ..runtime.tracing import force_traces_to_mill

        tracer_ctx = force_traces_to_mill(mill_repo)
    if tracer_ctx is None:
        from contextlib import nullcontext

        tracer_ctx = nullcontext()

    with tracer_ctx:
        digest = _build_cost_digest(settings)

        # 2. Memory + prior proposals
        memory_file = settings.memory_file_for("cost_analyst", board_id)
        memory = load_memory(memory_file)
        recent_proposals = _gather_recent_proposals(settings, board_id)

        # 3. Run the agent
        result = run_cost_analyst_agent(
            settings=settings,
            memory=memory,
            recent_proposals=recent_proposals,
            digest=digest,
        )

        # 4. File drafts
        created = _file_drafts(result, settings, session_id, board_id)

        # 5. Persist memory
        persist_memory(memory_file, result.updated_memory)

    return CostAnalystPassResult(
        updated_memory=result.updated_memory,
        drafts_created=created,
        session_id=session_id,
    )
