"""Run-health runner — global, cross-board run-registry monitor.

Two-phase (deterministic analysis → LLM interpretation, but
over the run registries instead of Langfuse cost):

    Phase 1 (deterministic, no LLM):
        Read EVERY registered board's run registry (``<data_dir>/<board_id>/
        runs.json``) READ-ONLY over the window, flag failed (``error``) and
        degraded (``ok`` whose summary matches a known degradation signal)
        runs, and group them by ``(kind, normalized signature)`` so a
        recurring failure is ONE candidate group (with a count), not one row
        per occurrence. Render the groups as a ``<run-health-candidates>``
        digest block.

    Phase 2 (LLM):
        Run the run-health agent (default tier) over the digest. It separates
        REAL failures from LEGITIMATE empty/no-op runs and emits one
        high-confidence draft per genuine failure group, filed to the mill
        board (deduplicated by normalized title AND by a ``gap-id`` marker
        against open ``source=run-health`` tickets).

Seam: tests monkeypatch ``run_run_health_agent`` and the registry-file reads.

NOTE: the digest path MUST NOT instantiate a second ``RunRegistry`` over a
board's ``runs.json`` — its ``_load()`` reconciles orphaned ``running``
entries to ``error`` and flushes, which would mutate the file the live worker
owns. We parse the JSON directly, read-only.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..agents.run_health import (
    MAX_PROPOSALS,
    RunHealthResult,
    run_run_health_agent,
)
from ..config import Settings, get_repos_config
from ..core.models import SourceKind
from ..core.service import TicketService
from ..core.dedup import normalize
from ..runners.pass_runner import load_memory, persist_memory

log = logging.getLogger("robotsix_mill.run_health")


# Case-insensitive substrings that mark an ``ok`` run as DEGRADED — a run
# that "succeeded" but whose summary signals a fetch/parse/exception problem
# or a suspicious empty result. Kept as a single named, commented constant so
# the list is easy to extend (do not scatter these literals).
DEGRADATION_SIGNALS: tuple[str, ...] = (
    "fetch skipped",
    "key missing",
    "parse error",
    "traceback",
    "exception",
    "no drafts created",
    "0 draft",
    "0 draft(s)",
)


# ---------------------------------------------------------------------------
# Phase 1 — deterministic cross-board run-health digest
# ---------------------------------------------------------------------------


@dataclass
class _Candidate:
    """One grouped failure/degradation, collapsing repeated occurrences."""

    kind: str
    board_id: str
    status: str
    signature: str  # normalized error signature
    count: int
    last_seen: str  # ISO-8601 of the most-recent occurrence
    last_dt: datetime
    sample: str  # raw summary/error text of the most-recent occurrence


def _read_registry_entries(path: Path) -> list[dict]:
    """Parse a board's ``runs.json`` READ-ONLY into a list of entry dicts.

    Missing or corrupt files are treated as empty (never raises). Crucially
    this does NOT instantiate ``RunRegistry`` — which would reconcile +
    rewrite the file the live worker owns.
    """
    try:
        data = json.loads(path.read_text())
    except OSError, json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict)]


def _parse_ts(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp into a UTC-aware datetime, or ``None``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _matches_degradation(summary: str) -> bool:
    s = (summary or "").lower()
    return any(sig in s for sig in DEGRADATION_SIGNALS)


def _normalize_signature(text: str) -> str:
    """Collapse transient specifics (paths, hex ids, digits, timestamps) so
    a recurring failure normalizes to one stable signature. Built on
    :func:`dedup.normalize`; clipped to ~80 chars."""
    s = (text or "").lower()
    s = re.sub(r"\S*/\S*", " ", s)  # path-like tokens (clone paths, urls)
    s = re.sub(r"\b[0-9a-f]{6,}\b", " ", s)  # hex ids / uuids
    s = re.sub(r"\d+", " ", s)  # digits / timestamps / counts
    return normalize(s)[:80]


def _collect_candidates(settings: Settings) -> list[_Candidate]:
    """Read every board's registry, flag candidates, and group them by
    ``(kind, normalized signature)``."""
    window = float(settings.run_health_window_hours)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window)
    groups: dict[tuple[str, str], _Candidate] = {}

    for repo in get_repos_config().repos.values():
        path = settings.data_dir / repo.repo_id / "runs.json"
        for entry in _read_registry_entries(path):
            started = _parse_ts(entry.get("started_at"))
            if started is None or started < cutoff:
                continue
            status = entry.get("status")
            # Running entries are in-flight, not candidates.
            if status not in ("ok", "error"):
                continue
            summary = entry.get("summary") or ""
            error = entry.get("error") or ""
            if status == "error":
                sig_source = error or summary
            else:  # status == "ok" — only the degraded ones qualify
                if not _matches_degradation(summary):
                    continue
                sig_source = summary
            signature = _normalize_signature(sig_source)
            kind = str(entry.get("kind") or "?")
            iso = entry.get("started_at") or ""
            key = (kind, signature)
            cand = groups.get(key)
            if cand is None:
                groups[key] = _Candidate(
                    kind=kind,
                    board_id=repo.repo_id,
                    status=str(status),
                    signature=signature,
                    count=1,
                    last_seen=iso,
                    last_dt=started,
                    sample=(sig_source or "").strip(),
                )
            else:
                cand.count += 1
                if started > cand.last_dt:
                    cand.last_dt = started
                    cand.last_seen = iso
                    cand.board_id = repo.repo_id
                    cand.status = str(status)
                    cand.sample = (sig_source or "").strip()

    return sorted(groups.values(), key=lambda c: (c.count, c.last_dt), reverse=True)


def _render_candidates(cands: list[_Candidate]) -> str:
    if not cands:
        return "(no failed or degraded runs in the window)"
    lines = [
        f"{len(cands)} candidate group(s) (recurring failures collapsed to one row).\n",
        "kind | board | status | count | last-seen | normalized signature | sample",
        "--- | --- | --- | --- | --- | --- | ---",
    ]
    for c in cands:
        sample = " ".join((c.sample or "").split())[:160]
        lines.append(
            f"{c.kind} | {c.board_id} | {c.status} | {c.count} | "
            f"{c.last_seen} | {c.signature} | {sample}"
        )
    specimens = []
    for c in cands[:3]:
        specimens.append(
            f"### {c.kind} on {c.board_id} "
            f"(status={c.status}, count={c.count})\n"
            f"- last seen: {c.last_seen}\n"
            f"- normalized signature: `{c.signature}`\n"
            f"- sample summary/error: {(c.sample or '(none)')[:500]}\n"
        )
    return "\n".join(lines) + "\n\n" + "\n".join(specimens)


def _build_run_health_digest(settings: Settings) -> str:
    """Build the run-health candidate digest as prompt text."""
    from ..agents.prompt_blocks import section

    cands = _collect_candidates(settings)
    return section("run-health-candidates", _render_candidates(cands))


# ---------------------------------------------------------------------------
# Phase 2 — file drafts (title + gap-id dedup against open run-health tickets)
# ---------------------------------------------------------------------------


_GAP_ID_RE = re.compile(r"<!--\s*run-health-gap-id:\s*(.+?)\s*-->")


def _ticket_body(service: TicketService, ticket: object) -> str:
    """Best-effort read of a ticket's description body (for gap-id dedup)."""
    try:
        return service.workspace(ticket).read_description() or ""  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 — best-effort dedup
        return ""


def _existing_markers(service: TicketService) -> tuple[set[str], set[str]]:
    """Normalized titles + embedded gap-ids of recent run-health tickets — a
    backstop against re-filing a proposal the agent already has."""
    title_keys: set[str] = set()
    gap_ids: set[str] = set()
    for t in service.recent_proposals_for(SourceKind.RUN_HEALTH, limit=200):
        title_keys.add(normalize(t.title)[:60])
        for m in _GAP_ID_RE.findall(_ticket_body(service, t)):
            gid = m.strip()
            if gid:
                gap_ids.add(gid)
    return title_keys, gap_ids


def _file_drafts(
    result: RunHealthResult,
    settings: Settings,
    session_id: str,
    board_id: str,
) -> list[dict]:
    service = TicketService(settings, board_id=board_id)
    seen_titles, seen_gaps = _existing_markers(service)
    created: list[dict] = []
    triples = list(
        zip(result.draft_titles, result.draft_bodies, result.gap_ids, strict=True)
    )
    for title, body, gap_id in triples[:MAX_PROPOSALS]:
        key = normalize(title)[:60]
        gid = (gap_id or "").strip()
        if key in seen_titles or (gid and gid in seen_gaps):
            log.info("run_health: skipping duplicate proposal %r", title)
            continue
        seen_titles.add(key)
        if gid:
            seen_gaps.add(gid)
        full_body = f"{body}\n\n<!-- run-health-gap-id: {gap_id} -->"
        try:
            ticket = service.create(
                title=title,
                description=full_body,
                source=SourceKind.RUN_HEALTH,
                origin_session=session_id,
            )
            created.append({"id": ticket.id, "title": ticket.title})
            log.info("run_health: filed run-health draft %r", ticket.id)
        except Exception:
            log.exception("run_health: failed to create draft %r", title)
    return created


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


@dataclass
class RunHealthPassResult:
    updated_memory: str
    drafts_created: list[dict]
    session_id: str


def _gather_recent_proposals(settings: Settings, board_id: str) -> str:
    from ..runners.pass_runner import _format_recent_proposals

    service = TicketService(settings, board_id=board_id)
    tickets = service.recent_proposals_for(SourceKind.RUN_HEALTH, limit=100)
    return _format_recent_proposals(tickets)


def run_run_health_pass(session_id: str) -> RunHealthPassResult:
    """Run a full run-health pass end-to-end.

    1. Build the cross-board run-health digest (deterministic, read-only).
    2. Load the run-health memory ledger + gather prior proposals.
    3. Run the run-health agent over the digest.
    4. File high-confidence drafts to the target (mill) board.
    5. Persist updated memory.
    """
    settings = Settings()
    board_id = settings.run_health_target_repo_id

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
        digest = _build_run_health_digest(settings)

        # 2. Memory + prior proposals
        memory_file = settings.memory_file_for("run_health", board_id)
        memory = load_memory(memory_file)
        recent_proposals = _gather_recent_proposals(settings, board_id)

        # 3. Run the agent
        try:
            result = run_run_health_agent(
                settings=settings,
                memory=memory,
                recent_proposals=recent_proposals,
                digest=digest,
            )
        except Exception:
            log.exception("run_health agent failed — returning empty result")
            return RunHealthPassResult(
                updated_memory=memory,
                drafts_created=[],
                session_id=session_id,
            )

        # 4. File drafts
        created = _file_drafts(result, settings, session_id, board_id)

        # 5. Persist memory
        persist_memory(memory_file, result.updated_memory)

    return RunHealthPassResult(
        updated_memory=result.updated_memory,
        drafts_created=created,
        session_id=session_id,
    )
