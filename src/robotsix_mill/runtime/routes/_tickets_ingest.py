"""Machine-facing ingestion endpoint with creation-time dedup.

``POST /tickets/ingest`` is designed for machine callers
(deployment/monitoring systems that re-report the same anomaly
periodically).  It applies a normalized-title fingerprint check,
a cheap token-overlap prefilter, and an LLM dedup check before
creating the ticket, so repeated reports of the same incident
do not create duplicate drafts.

When the board's open-ticket count reaches the configured cap
(``board_hygiene_max_open_tickets``), machine-ingest findings are
appended to a rollup epic instead of creating new standalone tickets.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...agents.dedup import (
    any_candidate_overlap,
    rank_candidates_by_similarity,
    run_dedup_check,
)
from ...agents.ops_classify import OpsClassifyVerdict, run_ops_classify_agent
from ...agents.runners.diagnostic_events import emit_diagnostic_event
from ...config import RepoConfig, ReposRegistry, Settings
from ...core.models import Ticket, TicketKind
from ...core.service import TicketService
from ...core.states import State
from ..deps import (
    get_repos_registry,
    get_settings,
    get_worker,
    maybe_enqueue,
)
from ..worker import Worker

logger = logging.getLogger(__name__)


router = APIRouter(tags=["Tickets"])

# Patterns stripped during title normalization for fingerprint dedup.
# Order matters: longest patterns first so shorter ones don't
# prematurely truncate (e.g. strip "20260731T155119Z-slug-a1b2" before
# the generic timestamp pattern).  All patterns are case-insensitive
# (titles are case-folded before matching).
_NORMALIZE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Full ticket-id pattern: YYYYMMDDTHHMMSSZ-slug-hex4
    (
        re.compile(
            r"\b\d{8}T\d{6}Z-[a-z0-9]+(?:-[a-z0-9]+)*-[a-f0-9]{4}\b", re.IGNORECASE
        ),
        " ",
    ),
    # ISO-8601 date + optional time: 2026-07-31, 2026-07-31T15:26:00Z
    (
        re.compile(
            r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?\b",
            re.IGNORECASE,
        ),
        " ",
    ),
    # Compact timestamp: 20260731, 20260731T155119Z
    (re.compile(r"\b\d{8}(?:T\d{6}Z)?\b", re.IGNORECASE), " "),
    # File paths with optional line numbers: src/foo/bar.py:123
    (
        re.compile(
            r"""(?:\S+/)+   # one or more path segments ending with /
                     \S+\.\w+    # filename with extension
                     (?::\d+)?   # optional :line_number
                     """,
            re.VERBOSE,
        ),
        " ",
    ),
    # Bare line-reference suffixes: :123, :45-67
    (re.compile(r":\d+(?:-\d+)?"), " "),
    # Hex suffixes often used as dedup counters: -a1b2, -deadbeef.
    # Anchored to a hyphen or underscore prefix so we don't strip
    # genuine English words whose letters happen to be all hex
    # (e.g. "feed", "dead", "face", "bead", "deed", "decade").
    (re.compile(r"[-_][a-f0-9]{4,8}\b", re.IGNORECASE), " "),
]


def _normalize_title(title: str) -> str:
    """Produce a fingerprint of *title* for duplicate detection.

    Case-folds, strips timestamps, file paths, line references, and
    hex suffixes, then collapses whitespace.  Two titles that describe
    the same symptom (e.g. "mail-ingester unhealthy on 2026-07-31"
    and "mail-ingester unhealthy on 2026-07-30") should produce the
    same fingerprint.
    """
    normalized = title.casefold()
    for pattern, replacement in _NORMALIZE_PATTERNS:
        normalized = pattern.sub(replacement, normalized)
    # Collapse runs of whitespace.
    normalized = re.sub(r"\s+", " ", normalized).strip()
    # Strip leading/trailing punctuation that survived.
    normalized = normalized.strip(".,;:!?-–— \t")
    return normalized


def _fingerprint_match(
    draft_title: str,
    candidates: list[Ticket],
) -> str | None:
    """Return the first candidate ticket id whose normalized title
    matches *draft_title*, or ``None``.
    """
    draft_fp = _normalize_title(draft_title)
    if not draft_fp:
        return None
    for t in candidates:
        candidate_fp = _normalize_title(t.title)
        if candidate_fp and candidate_fp == draft_fp:
            return str(t.id) if t.id is not None else None
    return None


# --- creation-time race guard ---------------------------------------------
#
# The dedup pass above is a check-then-create sequence, and the checks in
# between (candidate listing, token prefilter, LLM dedup) can take minutes
# under load.  Two identical reports that arrive inside that window both
# read a board without the ticket, both miss the fingerprint, and both
# create — which is how a single retried ``POST /tickets/ingest`` produced
# three copies of the same ticket.  Re-running the (cheap, deterministic)
# fingerprint check against freshly-listed candidates while holding a
# per-board lock closes the window without serialising the LLM call.

_BOARD_LOCKS: dict[str, threading.Lock] = {}
_BOARD_LOCKS_GUARD = threading.Lock()


def _board_lock(board_id: str) -> threading.Lock:
    """Return the process-wide ingest lock for *board_id*, creating it once."""
    with _BOARD_LOCKS_GUARD:
        lock = _BOARD_LOCKS.get(board_id)
        if lock is None:
            lock = threading.Lock()
            _BOARD_LOCKS[board_id] = lock
        return lock


def _create_ticket_guarded(
    body: TicketIngest,
    board_id: str,
    board_svc: TicketService,
    worker: Worker,
    settings: Settings,
    *,
    classification: str | None = None,
) -> JSONResponse:
    """Create the ticket, re-checking the title fingerprint under a lock.

    Every path that decided "this is not a duplicate" funnels through
    here.  Holding the board lock across the final re-check and the
    create makes concurrent identical ingests resolve to one ticket plus
    N-1 dedup hits, whatever the slower checks upstream concluded.
    """
    with _board_lock(board_id):
        fresh = [
            t
            for t in board_svc.list()
            if t.board_id == board_id and t.state not in {State.CLOSED, State.ERRORED}
        ]
        dup_id = _fingerprint_match(body.title, fresh)
        if dup_id is not None:
            board_svc.add_history_note(
                dup_id,
                f"re-reported by {body.source_tag} on {date.today().isoformat()} "
                "(fingerprint match, concurrent ingest)",
            )
            logger.info("ingest fingerprint match found on create-time re-check")
            return JSONResponse(
                status_code=200,
                content=IngestResult(ticket_id=dup_id, deduped=True).model_dump(),
            )
        return _create_ticket(
            body,
            board_id,
            board_svc,
            worker,
            settings,
            classification=classification,
        )


def _check_repo_workable(
    repo_config: RepoConfig,
    repo_id: str,
    settings: Settings,
) -> None:
    """Reject tickets for auto-registered repos when runtime
    registration is disabled (the repo was registered via
    POST /repos but the instance isn't configured to work it).
    """
    if repo_config.source == "auto" and not settings.allow_runtime_repo_registration:
        raise HTTPException(
            status_code=400,
            detail=f"Repo '{repo_id}' was registered at runtime but "
            "runtime repo registration is disabled. Tickets are only "
            "accepted for operator-configured repos.",
        )


class TicketIngest(BaseModel):
    """Payload for ``POST /tickets/ingest``."""

    repo_id: str
    title: str
    body: str
    source_tag: str  # free-form string identifying the machine caller


class IngestResult(BaseModel):
    """Response for ``POST /tickets/ingest``."""

    ticket_id: str
    deduped: bool
    classified: str | None = None


@router.post("/tickets/ingest")
def ingest_ticket(
    body: TicketIngest,
    worker: Worker = Depends(get_worker),
    settings: Settings = Depends(get_settings),
    repos: ReposRegistry = Depends(get_repos_registry),
) -> JSONResponse:
    """Create a ticket with creation-time dedup (``POST /tickets/ingest``).

    Returns 201 with ``deduped=False`` when a new ticket is created.
    Returns 200 with ``deduped=True`` when the report matches an
    existing ticket (a history note is appended to the existing one).
    Returns 404 when *repo_id* is not registered.
    """
    # 1. Repo validation — 404 for unknown repo_id.
    repo_config = repos.repos.get(body.repo_id)
    if repo_config is None:
        # The synthetic meta board lives in repos.meta (not repos.repos)
        # because it has no forge remote / per-repo config — but tickets
        # must still flow to it through the ingest path.
        if body.repo_id == "meta" and repos.meta is not None:
            repo_config = repos.meta
        else:
            raise HTTPException(
                status_code=404, detail=f"Unknown repo_id: {body.repo_id!r}"
            )

    # 2. Reject auto-registered repos when the flag is off.
    _check_repo_workable(repo_config, body.repo_id, settings)

    board_id = repo_config.board_id

    # 2b. Operational-maintenance classification — reject reports
    #     that describe manual operational actions (credential rotation,
    #     redeploy, infra console changes) rather than code work.
    ops_verdict = _run_ops_classify(body, board_id, settings)
    if ops_verdict is not None and ops_verdict.classification == "OPERATIONAL":
        logger.info(
            "ingest rejected as operational-maintenance: %s",
            ops_verdict.reason,
        )
        return JSONResponse(
            status_code=200,
            content={
                "filed": False,
                "reason": "operational-maintenance",
                "classification": "OPERATIONAL",
                "guidance": ops_verdict.reason,
            },
        )

    # Classification label for CODE reports (None if classifier failed).
    classification_label: str | None = (
        ops_verdict.classification if ops_verdict is not None else None
    )

    # 3. Candidate selection — scope to the target board.
    board_svc = TicketService(settings, board_id=board_id)
    all_tickets = board_svc.list()

    now = datetime.now(UTC)
    lookback_cutoff = now - timedelta(days=settings.dedup_lookback_days)
    candidates = [
        t
        for t in all_tickets
        if t.board_id == board_id
        and (
            t.state not in {State.CLOSED, State.ERRORED}
            or (t.state == State.CLOSED and t.updated_at >= lookback_cutoff)
        )
    ]

    if not candidates:
        return _create_ticket_guarded(
            body,
            board_id,
            board_svc,
            worker,
            settings,
            classification=classification_label,
        )

    # 4. Normalized-title fingerprint dedup — fast, deterministic,
    #    catches same-symptom reports across runs (e.g. "mail-ingester
    #    unhealthy on 2026-07-31" vs "mail-ingester unhealthy on
    #    2026-07-30").
    dup_id = _fingerprint_match(body.title, candidates)
    if dup_id is not None:
        board_svc.add_history_note(
            dup_id,
            f"re-reported by {body.source_tag} on {date.today().isoformat()} "
            "(fingerprint match)",
        )
        logger.info("ingest fingerprint match found")
        return JSONResponse(
            status_code=200,
            content=IngestResult(ticket_id=dup_id, deduped=True).model_dump(),
        )

    # 4. Cheap prefilter — skip LLM when no token overlap.
    candidate_texts: list[str] = []
    for t in candidates:
        try:
            desc = board_svc.workspace(t).read_description()
        except Exception:
            desc = ""
        candidate_texts.append(f"{t.title} {desc}")
    if not any_candidate_overlap(
        draft_title=body.title,
        draft_body=body.body,
        candidates_texts=candidate_texts,
    ):
        return _create_ticket_guarded(body, board_id, board_svc, worker, settings)

    # 5. LLM dedup.
    dup_id = _run_llm_dedup(body, candidates, board_svc, worker, settings)
    if dup_id is not None:
        board_svc.add_history_note(
            dup_id,
            f"re-reported by {body.source_tag} on {date.today().isoformat()}",
        )
        return JSONResponse(
            status_code=200,
            content=IngestResult(ticket_id=dup_id, deduped=True).model_dump(),
        )

    # already_done is deliberately not acted on — fall through to create.
    return _create_ticket_guarded(body, board_id, board_svc, worker, settings)


def _run_ops_classify(
    body: TicketIngest,
    board_id: str,
    settings: Settings,
) -> OpsClassifyVerdict | None:
    """Run the operational-maintenance classifier on the ingest body.

    Returns an :class:`OpsClassifyVerdict` on success, or ``None``
    when the LLM call fails (fail-open: a missed ops rejection is
    cheaper than a lost incident report).  Emits a diagnostic event
    so false-positives and false-negatives can be audited.
    """
    try:
        verdict = run_ops_classify_agent(
            settings=settings,
            title=body.title,
            body=body.body,
        )
    except Exception as exc:
        logger.warning(
            "ingest ops-classify failed, proceeding as code (fail-open): %s",
            exc,
        )
        return None

    # Record the classification decision for auditability.
    try:
        emit_diagnostic_event(
            settings=settings,
            board_id=board_id,
            category="OPS_CLASSIFY",
            ticket_id="",
            reason=(
                f"classification={verdict.classification} "
                f"title={body.title!r} reason={verdict.reason!r}"
            ),
            normalized_key=_normalize_title(body.title),
        )
    except Exception:
        logger.debug("ops-classify diagnostic event emission failed", exc_info=True)

    return verdict


def _run_llm_dedup(
    body: TicketIngest,
    candidates: list[Ticket],
    board_svc: TicketService,
    worker: Worker,
    settings: Settings,
) -> str | None:
    """Run the LLM dedup check against the top-ranked candidates.

    Returns a ``ticket_id`` when a duplicate is found, or ``None``
    when no duplicate was detected (fail-open: LLM errors also return
    ``None`` so the ticket is created).
    """
    top = rank_candidates_by_similarity(
        draft_title=body.title,
        draft_body=body.body,
        candidates=candidates,
        max_candidates=settings.dedup_max_candidates,
    )
    # Build candidates_json: one H2 section per candidate.
    lines: list[str] = []
    for t in top:
        try:
            desc = board_svc.workspace(t).read_description()
        except Exception:
            desc = ""
        snippet = desc[: settings.dedup_candidate_body_max_chars]
        lines.append(
            f"## {t.id}\n**Title**: {t.title}\n**State**: {t.state}\n\n{snippet}\n"
        )
    candidates_json = "\n".join(lines)

    try:
        verdict = run_dedup_check(
            settings=settings,
            draft_title=body.title,
            draft_body=body.body,
            candidates_json=candidates_json,
            repo_dir=None,
        )
    except Exception as exc:
        logger.warning("ingest dedup LLM failed, creating ticket (fail-open): %s", exc)
        return None

    return verdict.get("duplicate_of")


def _count_open_tickets(board_svc: TicketService, board_id: str) -> int:
    """Count non-terminal tickets on *board_id*.

    Terminal states: CLOSED, DONE, ANSWERED, EPIC_CLOSED, ERRORED.
    """
    all_tickets = board_svc.list()
    return sum(
        1
        for t in all_tickets
        if t.board_id == board_id
        and t.state
        not in {
            State.CLOSED,
            State.DONE,
            State.ANSWERED,
            State.EPIC_CLOSED,
            State.ERRORED,
        }
    )


_ROLLUP_TITLE_PREFIX = "Rollup: "


def _find_or_create_rollup_epic(
    board_svc: TicketService,
    board_id: str,
    source_tag: str,
) -> str:
    """Find or create an epic that rolls up findings for *source_tag*.

    Returns the ticket ID of the rollup epic.  If a matching epic
    already exists on *board_id* it is reused; otherwise a new
    EPIC_OPEN ticket is created.
    """
    rollup_title = f"{_ROLLUP_TITLE_PREFIX}{source_tag}"
    all_tickets = board_svc.list()
    for t in all_tickets:
        if (
            t.board_id == board_id
            and t.title == rollup_title
            and t.state == State.EPIC_OPEN
            and t.kind == TicketKind.EPIC
        ):
            return t.id
    # No existing rollup — create one.
    epic = board_svc.create(
        title=rollup_title,
        description=(
            f"Rollup epic for findings from ``{source_tag}``. "
            f"Individual findings are appended as history notes when "
            f"the board is at the open-ticket cap."
        ),
        source=source_tag,
        kind=TicketKind.EPIC,
        board_id=board_id,
    )
    return epic.id


def _create_ticket(
    body: TicketIngest,
    board_id: str,
    board_svc: TicketService,
    worker: Worker,
    settings: Settings,
    *,
    classification: str | None = None,
) -> JSONResponse:
    """Create a new draft ticket and enqueue it.  Shared between the
    dedup-miss path and the fail-open path.

    When the board's open-ticket count has reached the configured cap
    (``board_hygiene_max_open_tickets``), the finding is appended as a
    history note to a rollup epic instead of creating a new standalone
    ticket.
    """
    max_open = settings.board_hygiene_max_open_tickets
    if max_open > 0:
        open_count = _count_open_tickets(board_svc, board_id)
        if open_count >= max_open:
            rollup_id = _find_or_create_rollup_epic(
                board_svc, board_id, body.source_tag
            )
            note = (
                f"Finding deferred (open-ticket cap {max_open} reached): "
                f"**{body.title}**\n\n{body.body}"
            )
            board_svc.add_history_note(rollup_id, note)
            return JSONResponse(
                status_code=200,
                content=IngestResult(ticket_id=rollup_id, deduped=False).model_dump(),
            )

    ticket = board_svc.create(
        title=body.title,
        description=body.body,
        source=body.source_tag,
        kind=TicketKind.TASK,
        board_id=board_id,
    )
    maybe_enqueue(ticket, worker)
    return JSONResponse(
        status_code=201,
        content=IngestResult(
            ticket_id=ticket.id,
            deduped=False,
            classified=classification,
        ).model_dump(),
    )
