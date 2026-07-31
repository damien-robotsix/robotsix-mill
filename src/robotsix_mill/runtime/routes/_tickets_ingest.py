"""Machine-facing ingestion endpoint with creation-time dedup.

``POST /tickets/ingest`` is designed for machine callers
(deployment/monitoring systems that re-report the same anomaly
periodically).  It applies a cheap token-overlap prefilter followed
by an LLM dedup check before creating the ticket, so repeated
reports of the same incident do not create duplicate drafts.

When the board's open-ticket count reaches the configured cap
(``board_hygiene_max_open_tickets``), machine-ingest findings are
appended to a rollup epic instead of creating new standalone tickets.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...agents.dedup import (
    any_candidate_overlap,
    rank_candidates_by_similarity,
    run_dedup_check,
)
from ...config import RepoConfig, ReposRegistry, Settings
from ...core.models import TicketKind
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


def _check_repo_workable(
    repo_config: "RepoConfig",
    repo_id: str,
    settings: "Settings",
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
        raise HTTPException(
            status_code=404, detail=f"Unknown repo_id: {body.repo_id!r}"
        )

    # 2. Reject auto-registered repos when the flag is off.
    _check_repo_workable(repo_config, body.repo_id, settings)

    board_id = repo_config.board_id

    # 2. Candidate selection — scope to the target board.
    board_svc = TicketService(settings, board_id=board_id)
    all_tickets = board_svc.list()

    now = datetime.now(timezone.utc)
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
        return _create_ticket(body, board_id, board_svc, worker, settings)

    # 3. Cheap prefilter — skip LLM when no token overlap.
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
        return _create_ticket(body, board_id, board_svc, worker, settings)

    # 4. LLM dedup.
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
        return _create_ticket(body, board_id, board_svc, worker, settings)

    if verdict.get("duplicate_of"):
        dup_id: str = verdict["duplicate_of"]
        board_svc.add_history_note(
            dup_id,
            f"re-reported by {body.source_tag} on {date.today().isoformat()}",
        )
        return JSONResponse(
            status_code=200,
            content=IngestResult(ticket_id=dup_id, deduped=True).model_dump(),
        )

    # already_done is deliberately not acted on — fall through to create.
    return _create_ticket(body, board_id, board_svc, worker, settings)


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
        content=IngestResult(ticket_id=ticket.id, deduped=False).model_dump(),
    )
