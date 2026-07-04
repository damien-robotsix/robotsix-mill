"""Core ticket lifecycle routes."""

from __future__ import annotations

import json
import logging
import mimetypes
import threading
import time
from datetime import datetime, timezone

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    File,
    HTTPException,
    Request,
    UploadFile,
)


from ...core.models import (
    TicketCreate,
    TicketEvent,
    TicketKind,
    TicketMigrate,
    TicketRead,
    TicketTransition,
)
from ...config import RepoConfig, ReposRegistry, Settings

from ...core.models import Ticket
from ...core.service import TicketService
from ...core.states import State
from ..worker import Worker
from ..deps import (
    enrich_ticket_read,
    get_service,
    get_settings,
    get_worker,
    maybe_enqueue,
)
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
_LIST_CACHE: dict[tuple[str | None, bool, str], tuple[float, list[TicketRead]]] = {}
_LIST_CACHE_LOCK = threading.Lock()


def _repo_config_for_ticket(ticket: Ticket, repos: ReposRegistry) -> RepoConfig | None:
    """Resolve the ``RepoConfig`` for *ticket*'s ``board_id``.

    Returns ``None`` when the ticket has no ``board_id`` or the
    registry has no match (legacy tickets, single-repo mode).
    """
    if not ticket.board_id:
        return None
    for rc in repos.repos.values():
        if rc.repo_id == ticket.board_id:
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
    background: BackgroundTasks,
    state: State | None = None,
    include_closed: bool = False,
    repo_id: str | None = None,
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
    because the board polls this every few seconds.  A background
    cost-warming task refreshes the rows on each poll so subsequent
    requests show real values.

    An explicit *state* filter (e.g. ``state=closed``) takes
    precedence over the default exclusion — the terminal state is
    removed from the exclusion set so the explicit filter works as
    expected.
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
    cache_key = (state.value if state else None, include_closed, repo_id or "all")
    if ttl and ttl > 0.0:
        hit = _LIST_CACHE.get(cache_key)
        if hit is not None and (time.monotonic() - hit[0]) < ttl:
            return hit[1]
        with _LIST_CACHE_LOCK:
            hit = _LIST_CACHE.get(cache_key)
            if hit is not None and (time.monotonic() - hit[0]) < ttl:
                return hit[1]
            result = _list_tickets_compute(
                background, state, include_closed, repo_id, request, svc, settings
            )
            _LIST_CACHE[cache_key] = (time.monotonic(), result)
            return result
    return _list_tickets_compute(
        background, state, include_closed, repo_id, request, svc, settings
    )


def _list_tickets_compute(
    background: BackgroundTasks,
    state: State | None,
    include_closed: bool,
    repo_id: str | None,
    request: Request,
    svc: TicketService,
    settings: Settings,
) -> list[TicketRead]:
    """Build the enriched ticket list for :func:`list_tickets` (the cache
    miss / cache-disabled path). Kept separate so the cache wrapper stays
    a thin, obviously-correct guard around the expensive all-board fanout."""
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
            _TicketService(settings, board_id=rc.repo_id) for rc in repos.repos.values()
        ]
        # Include the synthetic meta board in the "all repos" view so
        # extraction proposals are never silently hidden.
        services.append(_TicketService(settings, board_id="meta"))

    tickets: list[Ticket] = []
    for s in services:
        try:
            tickets.extend(s.list(state=state, exclude_states=exclude))
        except Exception:
            log.exception("list_tickets: failed to query board %r", s.board_id)

    # Demand-driven cost warming (replaces the old cost_warmer daemon): the
    # list serves cost cache-only for speed, then fire-and-forgets a refresh
    # of the rows it just returned so the next poll shows real values. Runs
    # only when the board is actually being polled.
    from ..cost_warm import warm_ticket_costs

    rc_by_board = {rc.repo_id: rc for rc in repos.repos.values()}
    terminal = {State.CLOSED, State.EPIC_CLOSED}
    warm_items = [
        (t.id, rc_by_board.get(t.board_id)) for t in tickets if t.state not in terminal
    ]
    background.add_task(warm_ticket_costs, settings, warm_items)

    return [
        enrich_ticket_read(t, settings, svc, blocking_cost=False, fetch_pr_url=False)
        for t in tickets
    ]


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
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")
    repo_config = _repo_config_for_ticket(ticket, request.app.state.repos)
    return enrich_ticket_read(ticket, settings, svc, repo_config=repo_config)


@router.get("/tickets/{ticket_id}/history", response_model=list[TicketEvent])
def get_history(
    ticket_id: str,
    svc=Depends(get_service),
) -> list[TicketEvent]:
    """Return event history for a ticket (``GET /tickets/{ticket_id}/history``).

    Returns the ordered list of ``TicketEvent`` rows.  Raises 404 when
    the ticket does not exist.
    """
    if svc.get(ticket_id) is None:
        raise HTTPException(404, "ticket not found")
    return svc.history(ticket_id)


@router.get("/tickets/{ticket_id}/description")
def get_description(
    ticket_id: str,
    svc=Depends(get_service),
) -> dict:
    """Return the current description for a ticket (``GET /tickets/{ticket_id}/description``).

    Reads the description from the ticket's workspace on disk.
    Returns ``{"description": "..."}``.  Raises 404 when the ticket
    does not exist.
    """
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")
    return {"description": svc.workspace(ticket).read_description()}


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
) -> dict:
    """Attach an image screenshot to a ticket for the refine agent to view.

    Stores the bytes under the ticket's ``screenshots/`` directory (a
    sibling of ``artifacts/`` so a refine reset does not wipe user
    input). Rejects non-image uploads with 400 and unknown tickets with
    404. The filename is reduced to its basename to prevent traversal.
    """
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
) -> dict:
    """Return the retrospect.md artifact for a ticket, or empty if
    retrospect has not run yet (or the artifact was lost). Lets the
    board surface what retrospect actually wrote — without this the
    DONE -> CLOSED transition looks like it happened with no
    reflection, even when retrospect did run and write real analysis."""
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
) -> dict:
    """List artifact files in this ticket's workspace.

    Returns ``{"artifacts": [{"name": str, "size": int, "mtime": str},
    ...]}`` sorted by mtime ascending. Used by the board UI's drawer
    to surface each agent's output — pre-v1 the implement / refine /
    retrospect markdowns only existed on disk."""
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")
    ws = svc.workspace(ticket)
    d = ws.artifacts_dir
    items: list[dict] = []
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
                        tz=timezone.utc,
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
) -> dict:
    """Return the text content of a single artifact file.

    Refuses path-traversal (``..``, ``/``) so the route only serves
    files directly under the ticket's ``artifacts_dir``. Binary files
    return decoded-with-replace text since the drawer renders
    markdown / JSON; a hex viewer can be added later if needed."""
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
    404 if it doesn't exist."""
    if not svc.delete(ticket_id):
        raise HTTPException(404, "ticket not found")


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
    body: dict = Body(...),
    request: Request = None,
    svc=Depends(get_service),
    settings=Depends(get_settings),
) -> TicketRead:
    """Set the list of ticket IDs that *ticket_id* auto-unblocks when it
    completes (DONE/CLOSED/EPIC_CLOSED). Body: ``{"ticket_ids": [...]}``.

    Each listed ticket that is BLOCKED at that point is transitioned
    BLOCKED -> DRAFT. Cross-board safe. Returns the updated solver ticket.
    """
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
