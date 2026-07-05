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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlmodel import select

if TYPE_CHECKING:
    from ..agents.trace_inspector import TraceFinding

from ..config import RepoConfig, Settings
from ..core.db import session as db_session
from ..core.models import SourceKind, Ticket
from ..core.service import TicketService
from ..core.states import State
from ..core.dedup import find_prior_matching_ticket, normalize
from ..runtime.lifespan import _process_started_at

log = logging.getLogger("robotsix_mill.trace_review")


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
    is suppressed in that case."""

    cost_threshold: float | None
    obs_threshold: float | None
    cost_median: float | None
    obs_median: float | None


def _median(values: list[float]) -> float:
    sorted_v = sorted(values)
    n = len(sorted_v)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2 == 1:
        return float(sorted_v[mid])
    return (sorted_v[mid - 1] + sorted_v[mid]) / 2.0


def _compute_baselines(
    traces: list[dict],
    observations_per_trace: dict[str, list[dict]],
    settings: Settings,
) -> _Baselines:
    """Compute median × multiplier thresholds for cost and observation
    count across the entire *traces* batch. Returns ``None`` thresholds
    when the batch is too small to baseline (< 3 traces)."""
    if len(traces) < 3:
        return _Baselines(None, None, None, None)
    costs = [float(t.get("totalCost") or 0.0) for t in traces]
    obs_counts = [
        len(observations_per_trace.get(t.get("id") or "", [])) for t in traces
    ]
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
    trace: dict,
    observations: list[dict] | None,
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
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError, TypeError:
        return None


def _classify_trace(
    trace: dict,
    settings: Settings,
    observations: list[dict] | None = None,
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

    # Observation-level flags
    if (
        baselines is not None
        and baselines.obs_threshold is not None
        and len(observations) > baselines.obs_threshold
    ):
        flags.append(
            f"observation_storm ({len(observations)} obs vs "
            f"threshold {baselines.obs_threshold:.0f} = "
            f"{settings.trace_review_obs_multiplier:.1f}× "
            f"median {baselines.obs_median:.0f})"
        )

    # Per-tool call count + tool-error scan in a single pass.
    tool_calls: dict[str, int] = {}
    tool_errors = 0
    ask_user_calls = 0
    explore_runs = 0
    for o in observations:
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
            if name == "ask_user":
                pass
            # Successful run_command returns exit=0\n... or a success
            # sentence; skip the error-pattern regex for those so grep
            # output that contains test source code lines (e.g. "error:")
            # doesn't produce false positives.
            elif name == "run_command" and (
                out_s.startswith("exit=0\n") or "Your command ran successfully" in out_s
            ):
                pass
            # read_file path-not-found is user-friendly guidance for a
            # stale-path guess, not a structural failure.  Its standard
            # message begins with "error:" which would otherwise match
            # _TOOL_ERR_PATTERNS (\berror:).  Carve it out via the
            # stable substring "does not exist — try" from fs_tools.py.
            elif name == "read_file" and "does not exist — try" in out_s:
                pass
            # run_command empty-output failure (e.g. grep that finds
            # nothing, producing exit>0 + no output).  As of writing
            # this message does NOT match _TOOL_ERR_PATTERNS, but a
            # future addition to the pattern could start matching it.
            # Include a defensive carve-out keyed on the exact wording
            # from fs_tools.py run_command() so the classifier stays
            # robust regardless of pattern changes.  Do NOT suppress
            # exit=<rc>\n<stderr> returns — those carry real stderr and
            # must still be scanned.
            elif (
                name == "run_command"
                and "failed with exit code" in out_s
                and "and produced no output" in out_s
            ):
                pass
            # run_command rebase conflict notification
            # (e.g. "error: could not apply 57cd7900...")
            # is expected git output, not a tool failure.
            elif name == "run_command" and "error: could not apply" in out_s:
                pass
            # ticket_description retrieves the body text of existing
            # tickets, which may legitimately describe past tool errors
            # (e.g. "UsageLimitExceeded").  The tool itself succeeded
            # when it returned content; skip the error-pattern regex.
            elif name == "ticket_description":
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
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001 — corrupted state file = behave as no watermark
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
# Ticket dedup
# ---------------------------------------------------------------------------


def _existing_open_titles(service: TicketService, board_id: str) -> set[str]:
    """Return the set of normalized titles of still-open
    ``source=trace-review`` tickets on *service*'s board, so we don't
    re-file the same finding from a second flagged trace."""
    out: set[str] = set()
    settings = service.settings
    with db_session(settings, board_id) as s:
        stmt = (
            select(Ticket)
            .where(Ticket.source == SourceKind.TRACE_REVIEW)
            .where(Ticket.state != State.CLOSED)
        )
        if board_id:
            stmt = stmt.where(Ticket.board_id == board_id)
        for t in s.exec(stmt).all():
            out.add(normalize(t.title))
    return out


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
    except Exception:  # noqa: BLE001 — resolution is best-effort
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
        from ..agents.freshness import _resolve_path, extract_cited_paths

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
    except Exception:  # noqa: BLE001 — guard never blocks a legitimate filing
        log.debug("trace-review: path-existence guard failed", exc_info=True)
    return False, []


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass
class TraceReviewPassResult:
    """Summary of one trace-review pass."""

    drafts_created: list[dict] = field(default_factory=list)
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
    source_board_id = repo_config.repo_id
    # Findings are agent-side improvements (mill code, mill prompts),
    # not application-repo work. Route every draft to the configured
    # target board when set; fall back to the source repo's board only
    # in legacy deployments that haven't picked a target yet.
    target_board_id = source_board_id
    target_repo_id = repo_config.repo_id
    if settings.trace_review_target_repo_id:
        try:
            from ..config import get_repos_config

            registry = get_repos_config().repos
            target_rc = registry.get(settings.trace_review_target_repo_id)
            if target_rc is not None:
                target_board_id = target_rc.repo_id
                target_repo_id = settings.trace_review_target_repo_id
            else:
                log.warning(
                    "trace-review: configured target repo %r not "
                    "found — falling back to source board %r",
                    settings.trace_review_target_repo_id,
                    source_board_id,
                )
        except Exception:  # noqa: BLE001
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
    from ..langfuse.client import list_all_traces_since, fetch_trace_detail
    from ..agents.trace_inspector import run_trace_inspector

    now = datetime.now(timezone.utc)
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

    drafts: list[dict] = []
    flagged_count = 0
    # Snapshot open trace-review titles ONCE up front; we'll grow the
    # set as we file new drafts to avoid intra-run duplicates too.
    seen_titles = _existing_open_titles(service, target_board_id)

    # Pre-fetch every trace's full detail in one pass so we can compute
    # batch-relative baselines (median cost, median observation count)
    # before classifying any individual trace. Detail fetches are the
    # main cost of the deterministic phase — they're already paid for
    # by the classifier below, so caching them here is essentially
    # free.
    details_by_id: dict[str, dict] = {}
    observations_by_id: dict[str, list[dict]] = {}
    for trace in traces:
        trace_id = trace.get("id")
        if not trace_id:
            continue
        detail = fetch_trace_detail(
            settings,
            trace_id,
            repo_config=repo_config,
        )
        if detail is None:
            continue
        details_by_id[trace_id] = detail
        observations_by_id[trace_id] = detail.get("observations") or []

    baselines = _compute_baselines(traces, observations_by_id, settings)
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

    # Phase 1: classify every trace and collect flagged ones.
    flagged: list[tuple[dict[str, Any], _TraceFlags, dict[str, Any] | None]] = []
    for trace in traces:
        trace_id = trace.get("id")
        if not trace_id:
            continue
        detail = details_by_id.get(trace_id)
        observations = observations_by_id.get(trace_id)
        flags = _classify_trace(
            trace,
            settings,
            observations=observations,
            baselines=baselines,
            started_at=_process_started_at,
        )
        if not flags.flagged:
            continue
        flagged.append((trace, flags, detail))
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

    for idx, (trace, flags, detail) in enumerate(flagged):
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

        # Each finding -> one draft. Dedup against already-open titles.
        # Cap per-run so a noisy batch can't dump 50+ low-signal drafts;
        # cross-trace analysis is the right surface for recurring
        # patterns. Default cap = 5 per run (config-tunable via
        # trace_review_max_drafts_per_run).
        for finding in result.findings:
            if max_drafts > 0 and len(drafts) >= max_drafts:
                log.info(
                    "trace-review: hit per-run cap of %d drafts — "
                    "skipping remaining findings; bumped findings live "
                    "in Langfuse for the next cycle",
                    max_drafts,
                )
                break
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

            title = f"{finding.category} — {finding.symptom[:90]}"
            prior = find_prior_matching_ticket(
                service,
                target_board_id,
                finding.target_files,
                finding.symptom,
                settings,
                now,
                sources=[SourceKind.TRACE_REVIEW],
                lookback_days=settings.trace_review_dedup_lookback_days,
            )
            if prior is not None:
                log.info(
                    "trace-review: skipping draft for trace %s — matches prior "
                    "ticket %s (%s) in state %s",
                    trace_id[:8],
                    prior.id,
                    prior.title,
                    prior.state.value,
                )
                continue
            norm = normalize(title)
            if norm in seen_titles:
                log.debug(
                    "trace-review: skipping duplicate finding %r",
                    title,
                )
                continue
            seen_titles.add(norm)
            body = (
                f"_Filed by the periodic trace-review pass.  "
                f"Source trace: `{trace_id}` "
                f"(session `{flags.session_id or '(no session)'}`, "
                f"name `{flags.trace_name}`, total cost "
                f"${flags.total_cost:.4f})._\n\n"
                f"_Deterministic flags that surfaced this trace: "
                f"{', '.join(flags.flags)}._\n\n"
                "## Symptom\n\n"
                f"{finding.symptom}\n\n"
                "## Root cause (inspector hypothesis)\n\n"
                f"{finding.root_cause}\n\n"
                "## Proposed solution\n\n"
                f"{finding.proposed_solution}\n\n"
                f"_Inspector confidence: **{finding.confidence}**._\n"
            )
            try:
                ticket = service.create(
                    title=title,
                    description=body,
                    source=SourceKind.TRACE_REVIEW,
                    origin_session=session_id or None,
                )
                drafts.append({"id": ticket.id, "title": ticket.title})
            except Exception:  # noqa: BLE001
                log.exception(
                    "trace-review: failed to create draft for %r",
                    title,
                )
        # Also break out of the per-trace loop when the run-wide cap hit.
        if max_drafts > 0 and len(drafts) >= max_drafts:
            break

    # Persist the watermark at ``now``. Each run reviews the most recent
    # traces and intentionally skips any older backlog (see the lookback
    # clamp and newest-first fetch above), so there is no unprocessed tail to
    # preserve — advancing to ``now`` ensures the next run only looks at
    # traces produced since this one, keeping every run bounded.
    next_watermark = now
    try:
        _save_watermark(settings, source_board_id, next_watermark)
    except Exception:  # noqa: BLE001
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
