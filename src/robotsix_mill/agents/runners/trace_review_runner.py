"""Trace-review runner — periodic sweep over recent Langfuse traces.

Two-phase pipeline, designed to spend almost zero LLM tokens on healthy
traces and to bound the deep-inspection cost to the clearly-suspicious
ones:

    Phase 1 (deterministic):
        Walk every trace produced since the last run. Compute a set of
        boolean flags from the trace's own observations / cost / usage
        — no model calls. A trace with any flag set is forwarded to
        phase 2; everything else is dropped.

    Phase 2 (LLM):
        For each flagged trace, fetch the full observation tree and
        run the existing ``trace_inspector`` agent on a CHEAP (flash)
        model. Each returned ``TraceFinding`` becomes a draft ticket
        (deduplicated against still-open ``source=trace-review``
        tickets so a recurring symptom doesn't spawn a thousand drafts).

A monotonic ``last_run_at`` watermark is persisted per repo at
``<data_dir>/<board>/trace_review_state.json``. Subsequent runs only
scan traces created at or after that watermark; the first run uses
``trace_review_initial_lookback_hours``.

Seam: tests monkeypatch ``run_trace_inspector`` from
``robotsix_mill.agents.trace_inspector`` AND
``list_all_traces_since`` / ``fetch_trace_detail`` from
``robotsix_mill.langfuse.client``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ...agents.trace_inspector import TraceFinding

from ...config import RepoConfig, Settings
from ...core.models import SourceKind
from ...core.service import TicketService
from ...core.states import State
from ...runtime.lifespan import _process_started_at
from .trace_review_classifier import (
    _CONFIDENCE_RANK,
    _PER_OBS_COST_RE,
    _SELF_REFERENTIAL_TRACE_NAMES,
    _classify_trace,
    _compute_baselines,
    _dedupe_tool_spans,
    _TraceFlags,
)

log = logging.getLogger("robotsix_mill.trace_review")


# ---------------------------------------------------------------------------
# Watermark persistence
# ---------------------------------------------------------------------------


def _state_path(settings: Settings, board_id: str) -> Path:
    if board_id:
        return settings.data_dir / board_id / "trace_review_state.json"
    return settings.data_dir / "trace_review_state.json"


def _load_watermark(settings: Settings, board_id: str) -> datetime | None:
    p = _state_path(settings, board_id)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        ts = data.get("last_run_at")
        if not ts:
            return None
        return datetime.fromisoformat(ts)
    except Exception:
        log.warning("trace_review_state.json unreadable at %s — ignoring", p)
        return None


def _save_watermark(settings: Settings, board_id: str, when: datetime) -> None:
    p = _state_path(settings, board_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"last_run_at": when.isoformat()}, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Session → board resolution
# ---------------------------------------------------------------------------


def _extract_repo_from_session(session_id: str) -> str | None:
    """Extract the repo_id prefix from a qualified session id.

    Session format: ``<repo_id> · <kind>-<timestamp>-<hex>``
    Returns ``None`` for unqualified sessions (no `` · `` separator).
    """
    if not session_id:
        return None
    parts = session_id.split(" · ", 1)
    if len(parts) == 2 and parts[0]:
        return parts[0]
    return None


def _resolve_board_for_session(
    session_id: str, settings: Settings, fallback_board: str
) -> str:
    """Resolve the target board for a trace's session.

    Extracts *repo_id* from the session, looks it up in the repos
    registry, and returns its ``board_id``.  When the session is
    unqualified or the extracted repo is unknown, falls back to the
    mill board (``robotsix-mill`` in the registry); if even that
    fails, returns *fallback_board*.
    """
    repo_id = _extract_repo_from_session(session_id)
    if repo_id is None:
        return fallback_board
    try:
        from ...config import get_repos_config

        registry = get_repos_config().repos
        rc = registry.get(repo_id)
        if rc is not None and rc.board_id:
            return rc.board_id
    except Exception:
        log.debug("trace-review: repo registry lookup failed", exc_info=True)
    # Unknown repo — fall back to the mill board when possible.
    log.warning(
        "trace-review: session repo %r not in registry — falling back to mill board",
        repo_id,
    )
    try:
        from ...config import get_repos_config

        mill_rc = get_repos_config().repos.get("robotsix-mill")
        if mill_rc is not None and mill_rc.board_id:
            return mill_rc.board_id
    except Exception:
        log.debug("trace-review: mill board fallback lookup failed", exc_info=True)
    return fallback_board


# Matches <!-- {label}-gap-id: foo_bar --> style markers in ticket
# descriptions — same pattern as pass_runner._GAP_ID_RE.
_TRACE_GAP_ID_RE = re.compile(r"<!--\s*trace-review-gap-id:\s*(\S+)\s*-->")


def _trace_gap_id_exists(
    settings: Settings,
    board_id: str,
    trace_id: str,
) -> bool:
    """Return ``True`` when an open trace-review ticket on *board_id*
    already carries a ``<!-- trace-review-gap-id: {trace_id} -->``
    marker for *trace_id*.
    """
    try:
        svc = TicketService(settings, board_id=board_id)
        all_tickets = svc.list()
    except Exception:
        return False
    for t in all_tickets:
        if t.source != SourceKind.TRACE_REVIEW:
            continue
        if t.state in {State.CLOSED, State.ERRORED}:
            continue
        try:
            from ...core.workspace import Workspace

            desc = Workspace(
                settings.workspaces_dir_for(t.board_id), t.id
            ).read_description()
            for m in _TRACE_GAP_ID_RE.finditer(desc):
                if m.group(1) == trace_id:
                    return True
        except Exception:
            log.debug(
                "trace-review: gap-id check failed for ticket %s",
                t.id,
                exc_info=True,
            )
            continue
    return False


# ---------------------------------------------------------------------------
# Finding subject classification + root-cause fingerprint dedup
# ---------------------------------------------------------------------------

# Registry id of the mill repo — default destination for mill-machinery
# findings when ``trace_review_target_repo_id`` is unset.
_DEFAULT_MILL_REPO_ID = "robotsix-mill"

# Path prefixes that belong to the mill's own machinery (stages, prompts,
# tools, sandbox image, harness).
_MILL_PATH_PREFIXES = (
    "src/robotsix_mill/",
    "robotsix_mill/",
    "agent_definitions/",
    "expert_definitions/",
    "skills/",
    "sandbox/",
)

# Wording that ties a finding to mill machinery rather than to the
# inspected repo's own code.
_MILL_SUBJECT_RE = re.compile(
    r"\b(?:sandbox|harness|prompt|re-?prompt\w*|stage|pipeline|"
    r"robotsix[_-]mill|mill|agent[_ ]definitions?|structured output|"
    r"tool (?:call|layer|wrapper)|not installed|dockerfile)\b"
    r"|not found",
    re.IGNORECASE,
)

_MISSING_CMD_RE = re.compile(
    r"([A-Za-z][\w.+-]*):\s*(?:command\s+)?not found", re.IGNORECASE
)

_FINGERPRINT_RE = re.compile(r"<!--\s*trace-review-fingerprint:\s*(\S+)\s*-->")


def _finding_is_repo_local(finding: TraceFinding) -> bool:
    """Return ``True`` when *finding* is about the inspected repo's OWN
    code (e.g. a flaky test there) rather than mill machinery.

    Conservative: a finding is repo-local only when it cites concrete
    files, none of them mill paths, and its text does not point at a
    mill stage / prompt / tool / sandbox. Everything else is mill work.
    """
    files = [p.removeprefix("./") for p in (finding.target_files or []) if p]
    if not files:
        return False
    if any(p.startswith(_MILL_PATH_PREFIXES) for p in files):
        return False
    text = f"{finding.symptom}\n{finding.root_cause}\n{finding.proposed_solution}"
    return _MILL_SUBJECT_RE.search(text) is None


def _finding_fingerprint(finding: TraceFinding) -> str:
    """Stable root-cause fingerprint so the same gap seen in several
    traces (different sessions / repos) dedups to one ticket.
    """
    text = f"{finding.symptom}\n{finding.root_cause}"
    cmds = sorted({m.group(1).lower() for m in _MISSING_CMD_RE.finditer(text)})
    if cmds:
        return "missing-command:" + ",".join(cmds)
    norm = re.sub(r"\b[0-9a-f]{6,}\b", " ", finding.symptom.lower())
    words = re.sub(r"[^a-z]+", " ", norm).split()[:12]
    return f"{finding.category}:" + "-".join(words)


def _open_fingerprints(settings: Settings, board_id: str) -> set[str]:
    """Fingerprints carried by still-open trace-review tickets on *board_id*."""
    found: set[str] = set()
    try:
        tickets = TicketService(settings, board_id=board_id).list()
    except Exception:
        return found
    from ...core.workspace import Workspace

    for t in tickets:
        if t.source != SourceKind.TRACE_REVIEW:
            continue
        if t.state in {State.CLOSED, State.ERRORED}:
            continue
        try:
            desc = Workspace(
                settings.workspaces_dir_for(t.board_id), t.id
            ).read_description()
        except Exception:
            log.debug(
                "trace-review: fingerprint read failed for ticket %s",
                t.id,
                exc_info=True,
            )
            continue
        found.update(m.group(1) for m in _FINGERPRINT_RE.finditer(desc))
    return found


# ---------------------------------------------------------------------------
# Path-existence guard (deterministic, code-grounded)
# ---------------------------------------------------------------------------


def _resolve_target_repo_dir(
    settings: Settings,
    target_repo_id: str,
) -> Path | None:
    """Best-effort resolve a local checkout of *target_repo_id* whose
    source the findings reference, for deterministic path verification.

    Reuses any persistent workspace clone already on disk under
    ``<data_dir>/<repo_id>/<workspace>/repo`` (e.g. the roadmap-sync
    workspace); it NEVER clones. When no checkout is present the
    function returns ``None`` so every downstream path check degrades
    to a no-op — best-effort, never blocks a legitimate filing.
    """
    if not target_repo_id:
        return None
    try:
        repo_root = settings.data_dir / target_repo_id
        if not repo_root.is_dir():
            return None
        for workspace in sorted(repo_root.iterdir()):
            cand = workspace / "repo"
            if (cand / ".git").exists():
                return cand
    except Exception:
        log.debug("trace-review: repo_dir resolution failed", exc_info=True)
    return None


def _finding_cites_only_missing_paths(
    finding: TraceFinding,
    repo_dir: Path | None,
) -> tuple[bool, list[str]]:
    """Return ``(suppress, missing)`` for a single finding.

    ``suppress`` is True ONLY when the finding cites at least one
    concrete source path AND none of the cited paths resolve on HEAD —
    the documented false-positive mode (a finding about existing code
    that names files which do not exist). Cited paths are
    ``finding.target_files`` plus paths parsed from the finding text
    via :func:`freshness.extract_cited_paths`.

    Best-effort and exception-safe: when ``repo_dir`` is unresolved,
    no concrete path is cited, or any resolution/IO error occurs, the
    function returns ``(False, [])`` so filing proceeds unchanged.
    """
    if repo_dir is None:
        return False, []
    try:
        from ...agents.freshness import _resolve_path, extract_cited_paths

        text = f"{finding.symptom}\n{finding.root_cause}\n{finding.proposed_solution}"
        cited = list(finding.target_files or []) + extract_cited_paths(text)
        # De-duplicate while preserving order.
        seen: set[str] = set()
        paths: list[str] = []
        for raw in cited:
            if raw and raw not in seen:
                seen.add(raw)
                paths.append(raw)
        if not paths:
            return False, []

        missing: list[str] = []
        any_exists = False
        for raw in paths:
            _, exists, _, _ = _resolve_path(raw, repo_dir)
            if exists:
                any_exists = True
            else:
                missing.append(raw)
        if not any_exists:
            return True, missing
    except Exception:
        log.debug("trace-review: path-existence guard failed", exc_info=True)
    return False, []


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass
class TraceReviewPassResult:
    """Summary of one trace-review pass."""

    drafts_created: list[dict[str, Any]] = field(default_factory=list)
    traces_scanned: int = 0
    traces_flagged: int = 0
    window_start: str = ""
    window_end: str = ""
    summary: str = ""
    session_id: str = ""


def run_trace_review_pass(
    session_id: str = "",
    repo_config: RepoConfig | None = None,
) -> TraceReviewPassResult:
    """Execute one trace-review pass for *repo_config*'s board.

    Returns a :class:`TraceReviewPassResult` describing the window
    scanned, the number of traces scanned / flagged, and any drafts
    filed. Never raises — best-effort throughout.
    """
    settings = Settings()
    if repo_config is None:
        # Mono-repo mode is gone — every pass runs against a registered
        # repo so its source/target boards are well-defined.
        raise ValueError(
            "run_trace_review_pass: repo_config is required — "
            "configure at least one repo in config/repos.yaml."
        )
    source_board_id = repo_config.board_id
    # Findings are agent-side improvements (mill code, mill prompts),
    # not application-repo work. Route every draft to the configured
    # target board when set; fall back to the source repo's board only
    # in legacy deployments that haven't picked a target yet.
    target_board_id = source_board_id
    target_repo_id = repo_config.repo_id
    # True once the configured mill target resolved: mill-machinery
    # findings then always land there, whatever the session's repo.
    mill_target_resolved = False
    # Unset → default to the registry's ``robotsix-mill`` entry, so a
    # finding about mill's own stages/prompts/tools never lands on the
    # traced repo's board (where implement can only close it as
    # "wrong board").
    mill_repo_id = settings.trace_review_target_repo_id or _DEFAULT_MILL_REPO_ID
    if mill_repo_id:
        try:
            from ...config import get_repos_config

            registry = get_repos_config().repos
            target_rc = registry.get(mill_repo_id)
            if target_rc is not None and target_rc.board_id:
                target_board_id = target_rc.board_id
                target_repo_id = mill_repo_id
                mill_target_resolved = True
            elif settings.trace_review_target_repo_id:
                log.warning(
                    "trace-review: configured target repo %r not "
                    "found — falling back to source board %r",
                    settings.trace_review_target_repo_id,
                    source_board_id,
                )
        except Exception:
            log.exception(
                "trace-review: target-repo lookup failed; using source board",
            )
    service = TicketService(settings, board_id=target_board_id)
    # Best-effort local checkout of the TARGET repo (whose source the
    # findings reference) so the inspector's path-verification gate and
    # the deterministic guard below can read files. ``None`` when no
    # checkout is resolvable — every path check then degrades to a
    # no-op.
    repo_dir = _resolve_target_repo_dir(settings, target_repo_id)
    from ...agents.trace_inspector import run_trace_inspector
    from ...langfuse.client import fetch_trace_detail, list_all_traces_since

    now = datetime.now(UTC)
    # Watermark is per SOURCE board — each repo's Langfuse traces have
    # their own scan window. Dedup uses the TARGET board where the
    # tickets actually live.
    watermark = _load_watermark(settings, source_board_id)
    if watermark is None:
        watermark = now - timedelta(
            hours=settings.trace_review_initial_lookback_hours,
        )
    # Bound the lookback: never scan further back than the configured
    # lookback window, even when the persisted watermark is stale (e.g. a
    # prior run was interrupted before advancing it). Without this floor a
    # days-old watermark makes the fetch below paginate thousands of traces
    # in one pass, pegging the single-threaded event loop — the root cause
    # of the periodic unresponsiveness. The older traces are skipped
    # deliberately: trace-review is a recency-focused quality monitor, so
    # reviewing 3-day-old traces has little value and is not worth the cost.
    lookback_floor = now - timedelta(
        hours=settings.trace_review_initial_lookback_hours,
    )
    if watermark < lookback_floor:
        log.warning(
            "trace-review: watermark %s is older than the %dh lookback floor "
            "for %s — clamping to %s and skipping the stale backlog",
            watermark.isoformat(),
            settings.trace_review_initial_lookback_hours,
            repo_config.repo_id,
            lookback_floor.isoformat(),
        )
        watermark = lookback_floor
    window_start = watermark.isoformat()
    window_end = now.isoformat()

    # Fetch only the most recent ``cap`` traces (newest-first, early-stopping
    # pagination). The previous design fetched the ENTIRE window then drained
    # the OLDEST N per run, advancing the watermark a little each time — so a
    # large backlog took many heavy runs to clear AND reviewed stale traces
    # first. Reviewing the newest N and advancing the watermark to ``now``
    # (below) keeps each run bounded and focused on current behaviour.
    cap = settings.trace_review_max_traces_per_run
    traces = list_all_traces_since(
        settings,
        watermark.isoformat(),
        repo_config=repo_config,
        max_traces=cap if cap > 0 else None,
    )

    total_in_window = len(traces)
    if cap > 0 and total_in_window > cap:
        # Defensive: the fetch already bounds to the newest ``cap``, but if a
        # backend ignored the limit/ordering, keep only the most recent ``cap``.
        traces.sort(key=lambda t: t.get("timestamp") or "", reverse=True)
        traces = traces[:cap]
    if cap > 0 and len(traces) >= cap:
        log.info(
            "trace-review: reviewing the %d most recent traces for %s; "
            "any older traces beyond the cap are skipped (not queued)",
            len(traces),
            repo_config.repo_id,
        )

    log.info(
        "trace-review: %d traces in window %s → %s for %s",
        len(traces),
        window_start,
        window_end,
        repo_config.repo_id,
    )

    drafts: list[dict[str, Any]] = []
    flagged_count = 0
    # Track trace_ids already filed this run (gap-id dedup) to avoid
    # intra-run duplicates — one draft per trace, ever.
    filed_gap_ids: set[str] = set()
    # Root-cause fingerprints per destination board (open tickets +
    # filed this run) — one ticket per gap across sessions.
    fingerprints_by_board: dict[str, set[str]] = {}

    # Pass A (streaming): fetch each trace's detail only long enough to
    # derive the one lightweight metric the batch baseline needs — the
    # observation count — then let the payload go out of scope. Holding
    # every trace's full observation tree (inputs/outputs) at once was
    # the memory-growth root cause: a single pass over
    # ``trace_review_max_traces_per_run`` traces retained hundreds of
    # full payloads simultaneously and drove the process past the
    # container memcg limit (OOM-kill → restart → re-scan loop). Only
    # the small ``obs_counts_by_id`` mapping survives this loop.
    obs_counts_by_id: dict[str, int] = {}
    for trace in traces:
        trace_id = trace.get("id")
        if not trace_id:
            continue
        detail = fetch_trace_detail(settings, trace_id, repo_config=repo_config)
        if detail is None:
            continue
        obs_counts_by_id[trace_id] = len(
            _dedupe_tool_spans(detail.get("observations") or [])
        )
        # ``detail`` is intentionally not retained past this iteration.

    baselines = _compute_baselines(traces, obs_counts_by_id, settings)
    if baselines.cost_threshold is not None:
        log.info(
            "trace-review: cost baseline = $%.4f (median $%.4f × %.1f)",
            baselines.cost_threshold,
            baselines.cost_median,
            settings.trace_review_cost_multiplier,
        )
    else:
        log.info(
            "trace-review: batch too small (%d traces) — relative "
            "outlier flags suppressed; binary flags still active",
            len(traces),
        )

    # Phase 1 (streaming): re-fetch each trace's detail, classify, and
    # retain ONLY the lightweight ``_TraceFlags`` for flagged traces —
    # never the payload. The full detail for the (bounded) inspection
    # set is re-fetched on demand in phase 2 below, so at most one full
    # trace payload is ever held in memory at a time.
    flagged: list[tuple[dict[str, Any], _TraceFlags]] = []
    for trace in traces:
        trace_id = trace.get("id")
        if not trace_id:
            continue
        detail = fetch_trace_detail(settings, trace_id, repo_config=repo_config)
        observations = detail.get("observations") if detail else None
        flags = _classify_trace(
            trace,
            settings,
            observations=observations,
            baselines=baselines,
            started_at=_process_started_at,
        )
        if not flags.flagged:
            continue
        flagged.append((trace, flags))
        log.info(
            "trace-review: trace %s flagged (%s) — queued for inspection",
            trace_id[:8],
            ", ".join(flags.flags),
        )

    flagged_count = len(flagged)

    # Phase 2: inspect flagged traces in descending cost order,
    # bounded by trace_review_max_inspections_per_run.
    flagged.sort(key=lambda item: float(item[1].total_cost or 0.0), reverse=True)

    inspections_cap = settings.trace_review_max_inspections_per_run
    max_drafts = settings.trace_review_max_drafts_per_run
    min_confidence_rank = _CONFIDENCE_RANK.get(
        settings.trace_review_min_confidence, _CONFIDENCE_RANK["high"]
    )

    for idx, (trace, flags) in enumerate(flagged):
        if inspections_cap > 0 and idx >= inspections_cap:
            log.info(
                "trace-review: inspection cap of %d reached — "
                "%d flagged traces not inspected this run",
                inspections_cap,
                len(flagged) - idx,
            )
            break

        trace_id = flags.trace_id

        if flags.trace_name in _SELF_REFERENTIAL_TRACE_NAMES:
            log.info(
                "trace-review: skipping self-referential trace %s "
                "(name=%r) — trace_review pipeline traces are not filed",
                trace_id[:8],
                flags.trace_name,
            )
            continue

        # Re-fetch the full detail on demand — only for the bounded set
        # of traces actually inspected this run (capped by
        # ``trace_review_max_inspections_per_run``), so at most one full
        # trace payload is held in memory at a time rather than the whole
        # batch.
        detail = fetch_trace_detail(settings, trace_id, repo_config=repo_config)

        # Phase 2: LLM inspection on the cheap model.
        result = run_trace_inspector(
            settings=settings,
            trace_data=json.dumps(detail or trace, default=str),
            repo_dir=repo_dir,
            memory="",
            started_at=_process_started_at,
            classifier_flags=flags.flags,
        )
        if result.error:
            log.warning(
                "trace-review: inspector errored on %s: %s",
                trace_id[:8],
                result.error,
            )
            continue

        # Group all findings for this trace into ONE draft (one trace =
        # one ticket).  Per-finding noise-suppression guards still
        # apply — they filter individual findings out of the group.
        # When every finding is filtered, the trace produces no draft.
        kept_findings: list[TraceFinding] = []
        for finding in result.findings:
            # Confidence floor: drop findings below trace_review_min_confidence
            # (default "high"). Low/medium tool_error & agent_limitation
            # observations are telemetry, not actionable fixes, and flooded the
            # human-approval gate one-ticket-per-observation.
            if _CONFIDENCE_RANK.get(finding.confidence, 0) < min_confidence_rank:
                log.info(
                    "trace-review: dropping %s finding for trace %s below "
                    "confidence floor %s (finding was %s)",
                    finding.category,
                    trace_id[:8],
                    settings.trace_review_min_confidence,
                    finding.confidence,
                )
                continue
            # Deterministic path-existence guard: suppress findings
            # that cite source paths none of which exist on HEAD (the
            # documented false-positive mode — e.g. a fictional
            # ``robotsix_llmio/...`` path). No-op when repo_dir is
            # unresolved or the finding cites no concrete path.
            suppress, missing = _finding_cites_only_missing_paths(finding, repo_dir)
            if suppress:
                log.info(
                    "trace-review: suppressing finding for trace %s — all "
                    "cited source paths missing on HEAD: %s",
                    trace_id[:8],
                    ", ".join(missing),
                )
                continue

            # Suppress per-observation cost-outlier noise (covered by cost-analyst).
            if finding.category == "optimization" and _PER_OBS_COST_RE.search(
                finding.symptom
            ):
                log.info(
                    "trace-review: suppressing per-obs-cost optimization finding "
                    "for trace %s — not actionable",
                    trace_id[:8],
                )
                continue

            # Suppress weak optimization findings the inspector itself marked as
            # requiring human review (non-concrete proposed_solution).
            if (
                finding.category == "optimization"
                and finding.proposed_solution.startswith("REQUIRES_HUMAN_REVIEW:")
            ):
                log.info(
                    "trace-review: suppressing low-signal optimization finding "
                    "for trace %s — proposed_solution not concrete",
                    trace_id[:8],
                )
                continue

            kept_findings.append(finding)

        if not kept_findings:
            continue

        # Per-run draft cap — one draft per trace, so this bounds the
        # number of traces that can file.
        if max_drafts > 0 and len(drafts) >= max_drafts:
            log.info(
                "trace-review: hit per-run cap of %d drafts — "
                "skipping remaining traces; bumped traces live "
                "in Langfuse for the next cycle",
                max_drafts,
            )
            break

        # Route each finding by its subject: mill machinery (stages,
        # prompts, tools, sandbox image, harness) goes to the configured
        # mill target board; only findings about the inspected repo's own
        # code stay on the session's board. When no mill board resolves
        # the session's board receives everything (legacy routing).
        trace_session_board = _resolve_board_for_session(
            flags.session_id, settings, source_board_id
        )
        observed_repo = (
            _extract_repo_from_session(flags.session_id) or repo_config.repo_id
        )
        observed_ticket = (flags.session_id or "(no session)").split(" · ", 1)[-1]

        # Gap-id dedup: one draft per trace within a run.
        if trace_id in filed_gap_ids:
            log.debug(
                "trace-review: skipping trace %s — already filed this run",
                trace_id[:8],
            )
            continue
        filed_gap_ids.add(trace_id)

        by_board: dict[str, list[TraceFinding]] = {}
        for f in kept_findings:
            dest = (
                target_board_id
                if mill_target_resolved and not _finding_is_repo_local(f)
                else trace_session_board
            )
            by_board.setdefault(dest, []).append(f)

        for dest_board, board_findings in by_board.items():
            # Gap-id dedup across runs: stops re-filing the same trace.
            if _trace_gap_id_exists(settings, dest_board, trace_id):
                log.info(
                    "trace-review: skipping trace %s — gap-id already "
                    "exists on board %s",
                    trace_id[:8],
                    dest_board,
                )
                continue
            # Root-cause fingerprint dedup across sessions.
            if dest_board not in fingerprints_by_board:
                fingerprints_by_board[dest_board] = _open_fingerprints(
                    settings, dest_board
                )
            known = fingerprints_by_board[dest_board]
            fresh: list[tuple[TraceFinding, str]] = []
            for f in board_findings:
                fp = _finding_fingerprint(f)
                if fp in known:
                    log.info(
                        "trace-review: skipping %s finding for trace %s — "
                        "fingerprint %s already open on board %s",
                        f.category,
                        trace_id[:8],
                        fp,
                        dest_board,
                    )
                    continue
                known.add(fp)
                fresh.append((f, fp))
            if not fresh:
                continue

            title = (
                f"trace-review: {len(fresh)} finding(s) "
                f"for trace `{trace_id[:8]}` "
                f"(session: {flags.session_id or '(no session)'})"
            )
            findings_body = ""
            for i, (f, _fp) in enumerate(fresh, 1):
                findings_body += (
                    f"## Finding {i}: {f.category} — {f.symptom}\n\n"
                    f"**Root cause (inspector hypothesis):** {f.root_cause}\n\n"
                    f"**Proposed solution:** {f.proposed_solution}\n\n"
                    f"_Inspector confidence: **{f.confidence}**._\n\n"
                )
            markers = "".join(
                f"<!-- trace-review-fingerprint: {fp} -->\n" for _f, fp in fresh
            )
            body = (
                f"_Filed by the periodic trace-review pass.  "
                f"Source trace: `{trace_id}` "
                f"(session `{flags.session_id or '(no session)'}`, "
                f"name `{flags.trace_name}`, total cost "
                f"${flags.total_cost:.4f})._\n\n"
                f"Observed on: {observed_repo} {observed_ticket}\n\n"
                f"_Deterministic flags that surfaced this trace: "
                f"{', '.join(flags.flags)}._\n\n"
                f"{findings_body}"
                f"<!-- trace-review-gap-id: {trace_id} -->\n"
                f"{markers}"
            )
            try:
                board_service = (
                    service
                    if dest_board == target_board_id
                    else TicketService(settings, board_id=dest_board)
                )
                ticket = board_service.create(
                    title=title,
                    description=body,
                    source=SourceKind.TRACE_REVIEW,
                    origin_session=session_id or None,
                )
                drafts.append(
                    {"id": ticket.id, "title": ticket.title, "board": dest_board}
                )
            except Exception:
                log.exception(
                    "trace-review: failed to create draft for trace %s",
                    trace_id[:8],
                )

    # Persist the watermark at ``now``. Each run reviews the most recent
    # traces and intentionally skips any older backlog (see the lookback
    # clamp and newest-first fetch above), so there is no unprocessed tail to
    # preserve — advancing to ``now`` ensures the next run only looks at
    # traces produced since this one, keeping every run bounded.
    next_watermark = now
    try:
        _save_watermark(settings, source_board_id, next_watermark)
    except Exception:
        log.exception("trace-review: failed to persist watermark")

    summary = f"scanned={len(traces)} flagged={flagged_count} drafts={len(drafts)}"
    log.info("trace-review pass done: %s", summary)
    return TraceReviewPassResult(
        drafts_created=drafts,
        traces_scanned=len(traces),
        traces_flagged=flagged_count,
        window_start=window_start,
        window_end=window_end,
        summary=summary,
        session_id=session_id,
    )
