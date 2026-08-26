"""Core ticket lifecycle routes."""

from __future__ import annotations

import json
import logging
import mimetypes
import threading
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
)

from ...config import RepoConfig, ReposRegistry, Settings
from ...core.models import (
    Ticket,
    TicketCreate,
    TicketDescriptionUpdate,
    TicketEvent,
    TicketKind,
    TicketMigrate,
    TicketRead,
    TicketTransition,
)
from ...core.service import TicketService
from ...core.states import State
from ..deps import (
    enrich_ticket_read,
    get_service,
    get_settings,
    get_worker,
    maybe_enqueue,
    resolve_ticket_id,
)
from ..worker import Worker
from ._repo_helpers import _resolve_board_id

# Terminal states that are excluded from default listings (CLOSED,
# EPIC_CLOSED, ANSWERED).  These states have empty transition sets
# in the state machine and represent completed/archived work.
_LIST_TERMINAL_STATES: set[State] = {State.CLOSED, State.EPIC_CLOSED, State.ANSWERED}

log = logging.getLogger(__name__)

router = APIRouter(tags=["Tickets"])

# Short-TTL single-flight cache for the board-poll list endpoint. The board
# UI polls GET /tickets every ~5s and the board-manager agent polls it too;
# each call fans out an all-board query + enrichment, which under load piles
# up in the threadpool, contends on the GIL, and stalls every other request
# (the "API unresponsive while busy" failure). Collapsing repeated identical
# polls within a few seconds into one computation keeps the API responsive.
# Keyed by (state, include_closed, repo_id); guarded by a single lock so a
# burst of cache-miss pollers triggers ONE compute, not N concurrent ones.
_LIST_CACHE: dict[
    tuple[str | None, bool, str, int, int | None, str, str, str],
    tuple[float, list[TicketRead]],
] = {}
_LIST_CACHE_LOCK = threading.Lock()


def _repo_config_for_ticket(ticket: Ticket, repos: ReposRegistry) -> RepoConfig | None:
    """Resolve the ``RepoConfig`` for *ticket*'s ``board_id``.

    Returns ``None`` when the ticket has no ``board_id`` or the
    registry has no match (legacy tickets, single-repo mode).
    """
    if not ticket.board_id:
        return None
    for rc in repos.repos.values():
        if rc.board_id == ticket.board_id:
            return rc
    return None


@router.post("/tickets", response_model=TicketRead, status_code=201)
def create_ticket(
    body: TicketCreate,
    request: Request,
    svc=Depends(get_service),
    worker=Depends(get_worker),
    settings=Depends(get_settings),
) -> TicketRead:
    """Create a new ticket (``POST /tickets``).

    Resolves the board from *body.repo_id*, creates the ticket row
    plus workspace, enqueues it for the pipeline, and returns the
    enriched ``TicketRead``.  Returns 400 when the board cannot be
    resolved or the ticket data is invalid.
    """
    repos = request.app.state.repos
    board_id = _resolve_board_id(body.repo_id, repos)

    # Reject tickets for auto-registered repos when runtime
    # registration is disabled (the repo was registered via
    # POST /repos but the instance isn't configured to work it).
    if body.repo_id and body.repo_id in repos.repos:
        rc = repos.repos[body.repo_id]
        if rc.source == "auto" and not settings.allow_runtime_repo_registration:
            raise HTTPException(
                status_code=400,
                detail=f"Repo '{body.repo_id}' was registered at runtime but "
                "runtime repo registration is disabled. Tickets are only "
                "accepted for operator-configured repos.",
            )

    try:
        ticket = svc.create(
            body.title,
            body.description,
            source=body.source,
            depends_on=body.depends_on,
            unblocks=json.dumps(body.unblocks) if body.unblocks else None,
            kind=body.kind,
            parent_id=body.parent_id,
            board_id=board_id or None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    maybe_enqueue(ticket, worker)  # "directly taken in charge"
    return enrich_ticket_read(ticket, settings, svc)


@router.get("/tickets", response_model=list[TicketRead])
def list_tickets(
    state: State | None = None,
    include_closed: bool = False,
    repo_id: str | None = None,
    offset: int = 0,
    limit: int | None = None,
    sort_by: str = "created_at",
    created_after: str | None = None,
    updated_after: str | None = None,
    request: Request = None,
    svc=Depends(get_service),
    settings=Depends(get_settings),
) -> list[TicketRead]:
    """List tickets (``GET /tickets``).

    Returns the active tickets, optionally filtered by *state* and
    *repo_id*.  ``include_closed`` **defaults to False** — terminal
    states (CLOSED, EPIC_CLOSED, ANSWERED) are hidden; DONE stays
    visible (the transient retrospect window).  Closed/terminal
    tickets are the overwhelming majority of rows (>90 % on a mature
    board) and are not useful for board operation, so loading +
    enriching them on every poll is the dominant cost behind an
    unresponsive board; callers that genuinely need them must opt in
    with ``include_closed=true``.  Enrichment is downgraded for
    performance — cost is cache-only and PR URLs are skipped —
    because the board polls this every few seconds.  The Langfuse
    cost cache is warmed inline (parallelized via
    ThreadPoolExecutor) before enrichment, so every response shows
    real cost_usd values — not the misleading 0.0 that a pure
    background-task approach would return on cold caches.

    An explicit *state* filter (e.g. ``state=closed``) takes
    precedence over the default exclusion — the terminal state is
    removed from the exclusion set so the explicit filter works as
    expected.

    Pagination:
      *offset* — rows to skip (default 0).  *limit* — max rows to
      return (``None`` = unbounded / all rows).

    Sorting:
      *sort_by* — one of ``created_at``, ``updated_at``, ``title``,
      ``state``, ``priority``, ``kind`` (default ``created_at``).

    Filtering:
      *created_after* — ISO-8601 UTC datetime string; only tickets
      created strictly after this instant are returned.
      *updated_after* — ISO-8601 UTC datetime string; only tickets
      whose ``updated_at`` is strictly after this instant are
      returned.  Useful as a reconciliation mechanism for event
      subscribers that may have missed a delivery.
    """
    # The board polls this every 5s. Both expensive enrichments are
    # downgraded for the list:
    #   blocking_cost=False — cache-only Langfuse cost lookup (no HTTP).
    #   fetch_pr_url=False  — skip the per-ticket forge pr_status call.
    # On a cold cache with N review-state tickets, the full enrichment
    # would issue N Langfuse + N GitHub HTTP calls serially. The board
    # response would take longer than the poll interval, the next tick
    # would cancel its predecessor, and the board would never paint.
    # Per-ticket detail GETs keep both authoritative — when the user
    # opens the drawer they see real cost and a real PR link.
    #
    # include_closed=false hides terminal states (CLOSED, EPIC_CLOSED,
    # ANSWERED — the volume cases) but keeps DONE visible — DONE is
    # the transient retrospect-in-flight window and we want to watch
    # retrospect work without toggling.
    # Short-TTL single-flight cache (see _LIST_CACHE). On a fresh hit we
    # return the cached list without touching any DB. On a miss we hold the
    # lock across the compute so a burst of simultaneous pollers triggers
    # exactly one all-board query instead of one per request.
    ttl = settings.board_list_cache_ttl_seconds
    cache_key = (
        state.value if state else None,
        include_closed,
        repo_id or "all",
        offset,
        limit,
        sort_by,
        created_after or "",
        updated_after or "",
    )
    if ttl and ttl > 0.0:
        hit = _LIST_CACHE.get(cache_key)
        if hit is not None and (time.monotonic() - hit[0]) < ttl:
            return hit[1]
        with _LIST_CACHE_LOCK:
            hit = _LIST_CACHE.get(cache_key)
            if hit is not None and (time.monotonic() - hit[0]) < ttl:
                return hit[1]
            result = _list_tickets_compute(
                state,
                include_closed,
                repo_id,
                offset,
                limit,
                sort_by,
                created_after,
                updated_after,
                request,
                svc,
                settings,
            )
            _LIST_CACHE[cache_key] = (time.monotonic(), result)
            return result
    return _list_tickets_compute(
        state,
        include_closed,
        repo_id,
        offset,
        limit,
        sort_by,
        created_after,
        updated_after,
        request,
        svc,
        settings,
    )


def _list_tickets_compute(
    state: State | None,
    include_closed: bool,
    repo_id: str | None,
    offset: int,
    limit: int | None,
    sort_by: str,
    created_after: str | None,
    updated_after: str | None,
    request: Request,
    svc: TicketService,
    settings: Settings,
) -> list[TicketRead]:
    """Build the enriched ticket list for :func:`list_tickets` (the cache
    miss / cache-disabled path). Kept separate so the cache wrapper stays
    a thin, obviously-correct guard around the expensive all-board fanout.
    """
    exclude = None
    if not include_closed:
        exclude = set(_LIST_TERMINAL_STATES)
        # When the caller explicitly filters for a terminal state
        # (e.g. ``state=closed``), remove it from the exclusion set
        # so the explicit filter takes precedence — otherwise the
        # WHERE clause would be ``state='closed' AND state NOT IN
        # ('closed',…)``, which returns nothing.
        if state is not None and state in exclude:
            exclude.discard(state)
        if not exclude:
            exclude = None

    # Parse created_after from ISO-8601 string to UTC datetime.
    # Accept both naive and timezone-aware strings; treat naive as UTC
    # so the TZDateTime column type (which rejects naive datetimes)
    # can bind the value correctly.
    created_after_dt: datetime | None = None
    if created_after:
        try:
            created_after_dt = datetime.fromisoformat(created_after)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"created_after must be an ISO-8601 datetime string, "
                f"got {created_after!r}",
            ) from None
        if created_after_dt.tzinfo is None:
            created_after_dt = created_after_dt.replace(tzinfo=UTC)

    # Parse updated_after from ISO-8601 string to UTC datetime.
    updated_after_dt: datetime | None = None
    if updated_after:
        try:
            updated_after_dt = datetime.fromisoformat(updated_after)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"updated_after must be an ISO-8601 datetime string, "
                f"got {updated_after!r}",
            ) from None
        if updated_after_dt.tzinfo is None:
            updated_after_dt = updated_after_dt.replace(tzinfo=UTC)

    # With per-repo DBs the default svc only sees its own board's
    # tickets. Build a list of services to query: one per repo when
    # repo_id is omitted or "all", else just the requested repo.
    from ...core.service import TicketService as _TicketService

    repos = request.app.state.repos
    if repo_id and repo_id != "all":
        board_id = _resolve_board_id(repo_id, repos)
        services = [_TicketService(settings, board_id=board_id)]
    else:
        services = [
            _TicketService(settings, board_id=rc.board_id)
            for rc in repos.repos.values()
        ]
        # Include the synthetic meta board in the "all repos" view so
        # extraction proposals are never silently hidden.
        services.append(_TicketService(settings, board_id="meta"))

    # When offset/limit/sort is in play across multiple boards we can't
    # trivially merge and re-slice — each board query is independent.
    # Apply offset+limit+sort per-board and concatenate; callers needing
    # cross-board ordering should use a single board filter (repo_id).
    tickets: list[Ticket] = []
    for s in services:
        try:
            tickets.extend(
                s.list(
                    state=state,
                    exclude_states=exclude,
                    offset=offset,
                    limit=limit,
                    sort_by=sort_by,
                    created_after=created_after_dt,
                    updated_after=updated_after_dt,
                )
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception:
            log.exception("list_tickets: failed to query board %r", s.board_id)

    # Inline cost warming: warm the Langfuse cost cache for every
    # non-terminal ticket *before* enriching, so the cache-only cost
    # lookup in enrichment returns real values (not 0.0). The warmer
    # uses a ThreadPoolExecutor internally, giving us parallelized
    # Langfuse calls instead of a serial N+1.  The old approach used
    # a BackgroundTask for this, which returned 0.0 on every cold
    # first poll — making the list endpoint's cost_usd field
    # perennially wrong for all but the most-heavily-polled boards.
    from ..cost_warm import warm_ticket_costs

    rc_by_board = {rc.board_id: rc for rc in repos.repos.values()}
    terminal = {State.CLOSED, State.EPIC_CLOSED}
    warm_items = [
        (t.id, rc_by_board.get(t.board_id)) for t in tickets if t.state not in terminal
    ]
    warm_ticket_costs(settings, warm_items)

    enriched: list[TicketRead] = []
    for t in tickets:
        try:
            enriched.append(
                enrich_ticket_read(
                    t, settings, svc, blocking_cost=False, fetch_pr_url=False
                )
            )
        except Exception:
            log.exception(
                "list_tickets: skipping ticket %s due to enrichment error", t.id
            )
    return enriched


@router.get("/tickets/{ticket_id}", response_model=TicketRead)
def get_ticket(
    ticket_id: str,
    request: Request,
    svc=Depends(get_service),
    settings=Depends(get_settings),
) -> TicketRead:
    """Return a single ticket (``GET /tickets/{ticket_id}``).

    Returns the fully enriched ``TicketRead`` (with cost and PR link).
    Raises 404 when the ticket does not exist.
    """
    ticket_id = resolve_ticket_id(ticket_id, svc)
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")
    repo_config = _repo_config_for_ticket(ticket, request.app.state.repos)
    return enrich_ticket_read(ticket, settings, svc, repo_config=repo_config)


@router.get("/tickets/{ticket_id}/history", response_model=list[TicketEvent])
def get_history(
    ticket_id: str,
    limit: int | None = Query(None, ge=1),
    offset: int = Query(0, ge=0),
    order: str = Query("asc", pattern="^(asc|desc)$"),
    svc=Depends(get_service),
) -> list[TicketEvent]:
    """Return event history for a ticket (``GET /tickets/{ticket_id}/history``).

    Returns ``TicketEvent`` rows ordered by ``at``.  Query params:

    * ``limit`` — max events to return (unbounded when omitted).
    * ``offset`` — events to skip before the first returned event.
    * ``order`` — ``asc`` (chronological, default) or ``desc``
      (most-recent-first — useful for retrieving the final event).
    Raises 404 when the ticket does not exist.
    """
    ticket_id = resolve_ticket_id(ticket_id, svc)
    if svc.get(ticket_id) is None:
        raise HTTPException(404, "ticket not found")
    return svc.history(ticket_id, limit=limit, offset=offset, order=order)


@router.get("/tickets/{ticket_id}/description")
def get_description(
    ticket_id: str,
    svc=Depends(get_service),
) -> dict[str, Any]:
    """Return the current description for a ticket (``GET /tickets/{ticket_id}/description``).

    Reads the description from the ticket's workspace on disk.
    Returns ``{"description": "..."}``.  Raises 404 when the ticket
    does not exist.
    """
    ticket_id = resolve_ticket_id(ticket_id, svc)
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")
    return {"description": svc.workspace(ticket).read_description()}


@router.put("/tickets/{ticket_id}/description")
def update_description(
    ticket_id: str,
    body: TicketDescriptionUpdate,
    svc=Depends(get_service),
) -> dict[str, Any]:
    """Update a ticket's spec description (``PUT /tickets/{ticket_id}/description``).

    Replaces the ticket's ``description.md``, recomputes the spec
    fingerprint so the implement stage's stale-respawn guard allows a
    fresh attempt, and records a history event with old/new fingerprint
    and author.

    Body: ``{"description": "<new spec>", "reset_fingerprint_guard": false, "author": "operator"}``.

    Raises 404 when the ticket does not exist, 409 when the ticket is
    in a terminal state (CLOSED, ANSWERED, EPIC_CLOSED, DONE).
    """
    if svc.get(ticket_id) is None:
        raise HTTPException(404, "ticket not found")
    try:
        event = svc.update_description(
            ticket_id,
            body.description,
            reset_fingerprint_guard=body.reset_fingerprint_guard,
            author=body.author,
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    ticket = svc.get(ticket_id)
    return {
        "ticket_id": ticket_id,
        "content_hash": ticket.content_hash,
        "fingerprint_reset": body.reset_fingerprint_guard,
        "event_id": event.id,
    }


# Supported screenshot image media types (content-type → canonical).
_SCREENSHOT_MEDIA_TYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
}

# Reject screenshot uploads larger than this (10 MiB) before writing.
_MAX_SCREENSHOT_BYTES = 10 * 1024 * 1024


@router.post("/tickets/{ticket_id}/screenshots", status_code=201)
async def upload_screenshot(
    ticket_id: str,
    file: UploadFile = File(...),
    svc=Depends(get_service),
) -> dict[str, Any]:
    """Attach an image screenshot to a ticket for the refine agent to view.

    Stores the bytes under the ticket's ``screenshots/`` directory (a
    sibling of ``artifacts/`` so a refine reset does not wipe user
    input). Rejects non-image uploads with 400 and unknown tickets with
    404. The filename is reduced to its basename to prevent traversal.
    """
    ticket_id = resolve_ticket_id(ticket_id, svc)
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")

    media_type = file.content_type
    if media_type not in _SCREENSHOT_MEDIA_TYPES:
        guessed, _ = mimetypes.guess_type(file.filename or "")
        media_type = guessed
    if media_type not in _SCREENSHOT_MEDIA_TYPES:
        raise HTTPException(400, "upload must be an image (png, jpeg, gif, webp)")

    # Basename only — strip any directory components to prevent traversal.
    raw_name = (file.filename or "").replace("\\", "/").split("/")[-1].strip()
    if not raw_name or raw_name in (".", ".."):
        ext = mimetypes.guess_extension(media_type) or ".png"
        existing = len(svc.workspace(ticket).list_screenshots())
        raw_name = f"screenshot-{existing + 1}{ext}"

    data = await file.read()
    if len(data) > _MAX_SCREENSHOT_BYTES:
        raise HTTPException(413, "screenshot exceeds the 10 MiB size limit")

    dest = svc.workspace(ticket).screenshots_dir / raw_name
    try:
        dest.write_bytes(data)
    except OSError as exc:
        raise HTTPException(500, "failed to save screenshot") from exc
    return {"filename": raw_name, "ticket_id": ticket_id}


@router.get("/tickets/{ticket_id}/retrospect")
def get_retrospect(
    ticket_id: str,
    svc=Depends(get_service),
) -> dict[str, Any]:
    """Return the retrospect.md artifact for a ticket, or empty if
    retrospect has not run yet (or the artifact was lost). Lets the
    board surface what retrospect actually wrote — without this the
    DONE -> CLOSED transition looks like it happened with no
    reflection, even when retrospect did run and write real analysis.
    """
    ticket_id = resolve_ticket_id(ticket_id, svc)
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")
    ws = svc.workspace(ticket)
    p = ws.artifacts_dir / "retrospect.md"
    if not p.exists():
        return {"retrospect": ""}
    return {"retrospect": p.read_text(encoding="utf-8")}


# Artifact filename → stage that produced it. Drives the v1 drawer
# expanded view: a history row whose stage owns a file gets a
# "details" button that fetches that file via the route below.
# Listed once here so the UI and the listing endpoint stay in sync.
_STAGE_ARTIFACTS: dict[str, list[str]] = {
    "refine": [
        "draft-original.md",
        "file_map.json",
        "refine-verbose.md",
        "epic-body-proposed.md",
    ],
    "implement": ["implement.md", "implement_summary.md", "reference_files.json"],
    "review": ["review.md"],
    "document": [],
    "deliver": ["deliver.md"],
    "merge": ["merge.md", "merge_reason.txt", "review_feedback.json"],
    "retrospect": ["retrospect.md"],
    "answer": ["question-original.md"],
    "ci_fix": ["ci_fix.md", "failing_summary.txt"],
}


@router.get("/tickets/{ticket_id}/artifacts")
def list_artifacts(
    ticket_id: str,
    svc=Depends(get_service),
) -> dict[str, Any]:
    """List artifact files in this ticket's workspace.

    Returns ``{"artifacts": [{"name": str, "size": int, "mtime": str},
    ...]}`` sorted by mtime ascending. Used by the board UI's drawer
    to surface each agent's output — pre-v1 the implement / refine /
    retrospect markdowns only existed on disk.
    """
    ticket_id = resolve_ticket_id(ticket_id, svc)
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")
    ws = svc.workspace(ticket)
    d = ws.artifacts_dir
    items: list[dict[str, Any]] = []
    if d.exists():
        for p in d.iterdir():
            if not p.is_file():
                continue
            try:
                stat = p.stat()
            except OSError:
                continue
            items.append(
                {
                    "name": p.name,
                    "size": stat.st_size,
                    "mtime": datetime.fromtimestamp(
                        stat.st_mtime,
                        tz=UTC,
                    )
                    .isoformat()
                    .replace("+00:00", "Z"),
                }
            )
    items.sort(key=lambda x: x["mtime"])
    return {"artifacts": items}


@router.get("/tickets/{ticket_id}/artifacts/{name}")
def get_artifact(
    ticket_id: str,
    name: str,
    svc=Depends(get_service),
) -> dict[str, Any]:
    """Return the text content of a single artifact file.

    Refuses path-traversal (``..``, ``/``) so the route only serves
    files directly under the ticket's ``artifacts_dir``. Binary files
    return decoded-with-replace text since the drawer renders
    markdown / JSON; a hex viewer can be added later if needed.
    """
    ticket_id = resolve_ticket_id(ticket_id, svc)
    if "/" in name or ".." in name or name.startswith("."):
        raise HTTPException(400, "invalid artifact name")
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")
    p = svc.workspace(ticket).artifacts_dir / name
    if not p.is_file():
        raise HTTPException(404, "artifact not found")
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise HTTPException(500, f"read failed: {e}") from None
    return {"name": name, "content": content}


@router.delete("/tickets/{ticket_id}", status_code=204)
def delete_ticket(
    ticket_id: str,
    svc=Depends(get_service),
) -> None:
    """Hard-delete a ticket (row + history + workspace). Irreversible.
    404 if it doesn't exist.
    """
    ticket_id = resolve_ticket_id(ticket_id, svc)
    if not svc.delete(ticket_id):
        raise HTTPException(404, "ticket not found")
    log.info("Deleted ticket %r (agent: robotsix-chat)", ticket_id)


@router.post("/tickets/{ticket_id}/transition", response_model=TicketRead)
def transition(
    ticket_id: str,
    body: TicketTransition,
    request: Request,
    svc=Depends(get_service),
    worker=Depends(get_worker),
    settings=Depends(get_settings),
) -> TicketRead:
    """Transition a ticket to a new state (``POST /tickets/{ticket_id}/transition``).

    Body: ``{"state": "<state>", "note": "<optional note>"}``.
    Enqueues the ticket after transition so the pipeline picks it up.
    Returns the enriched ``TicketRead``.  Raises 404 when the ticket
    does not exist.
    """
    ticket_id = resolve_ticket_id(ticket_id, svc)
    try:
        ticket = svc.transition(ticket_id, body.state, body.note)
    except KeyError:
        raise HTTPException(404, "ticket not found") from None
    maybe_enqueue(ticket, worker)  # human unblock re-triggers the chain
    repo_config = _repo_config_for_ticket(ticket, request.app.state.repos)
    return enrich_ticket_read(ticket, settings, svc, repo_config=repo_config)


@router.post("/tickets/{ticket_id}/migrate", response_model=TicketRead)
def migrate_ticket(
    ticket_id: str,
    body: TicketMigrate,
    request: Request,
    svc: TicketService = Depends(get_service),
    worker: Worker = Depends(get_worker),
    settings: Settings = Depends(get_settings),
) -> TicketRead:
    """Move a ticket to another board (row, history, comments, workspace).

    For tickets filed on the wrong board (the fix belongs to a
    different repo). The migrated ticket lands in DRAFT on the target
    board so its refine stage re-triages it there.
    """
    ticket_id = resolve_ticket_id(ticket_id, svc)
    repos = request.app.state.repos
    board_id = _resolve_board_id(body.repo_id, repos)
    try:
        ticket = svc.migrate(ticket_id, board_id, note=body.note)
    except KeyError:
        raise HTTPException(404, "ticket not found") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    maybe_enqueue(ticket, worker)  # draft → refine on the new board
    repo_config = _repo_config_for_ticket(ticket, repos)
    return enrich_ticket_read(ticket, settings, svc, repo_config=repo_config)


@router.post("/tickets/{ticket_id}/unblocks", response_model=TicketRead)
def set_unblocks(
    ticket_id: str,
    body: dict[str, Any] = Body(...),
    request: Request = None,
    svc=Depends(get_service),
    settings=Depends(get_settings),
) -> TicketRead:
    """Set the list of ticket IDs that *ticket_id* auto-unblocks when it
    completes (DONE/CLOSED/EPIC_CLOSED). Body: ``{"ticket_ids": [...]}``.

    Each listed ticket that is BLOCKED at that point is transitioned
    BLOCKED -> DRAFT. Cross-board safe. Returns the updated solver ticket.
    """
    ticket_id = resolve_ticket_id(ticket_id, svc)
    raw = body.get("ticket_ids", [])
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise HTTPException(400, "ticket_ids must be a list of strings")
    try:
        ticket = svc.set_unblocks(ticket_id, raw)
    except KeyError:
        raise HTTPException(404, "ticket not found") from None
    repo_config = _repo_config_for_ticket(ticket, request.app.state.repos)
    return enrich_ticket_read(ticket, settings, svc, repo_config=repo_config)


@router.post("/tickets/{ticket_id}/approve", response_model=TicketRead)
def approve_ticket(
    ticket_id: str,
    request: Request,
    svc=Depends(get_service),
    worker=Depends(get_worker),
    settings=Depends(get_settings),
) -> TicketRead:
    """Human approval for a ticket (``POST /tickets/{ticket_id}/approve``).

    Transitions the ticket to READY and enqueues it so implement picks
    it up.  If the ticket has an epic parent and a proposed epic body
    artifact exists (``epic-body-proposed.md``), that body is applied
    to the epic as a best-effort side effect.  Returns 404 when the
    ticket does not exist.
    """
    ticket_id = resolve_ticket_id(ticket_id, svc)
    try:
        ticket = svc.transition(ticket_id, State.READY, note="approved by human")
    except KeyError:
        raise HTTPException(404, "ticket not found") from None

    # If this ticket has an epic parent, check for a proposed epic body
    # artifact and apply it to the epic on approval.
    try:
        if ticket.parent_id:
            parent = svc.get(ticket.parent_id)
            if parent is not None and parent.kind == TicketKind.EPIC:
                artifact = svc.workspace(ticket).artifacts_dir / "epic-body-proposed.md"
                if artifact.exists():
                    epic_body = artifact.read_text(encoding="utf-8").strip()
                    if epic_body:
                        new_hash = svc.workspace(parent).write_description(epic_body)
                        svc.set_content_hash(parent.id, new_hash)
    except Exception:
        pass  # best-effort: approval always succeeds

    maybe_enqueue(ticket, worker)  # implement picks it up from ready
    repo_config = _repo_config_for_ticket(ticket, request.app.state.repos)
    return enrich_ticket_read(ticket, settings, svc, repo_config=repo_config)
