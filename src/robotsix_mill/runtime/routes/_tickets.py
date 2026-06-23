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

from ...agents.ask_to_ticket import run_ask_to_ticket_agent
from ...core.models import (
    CommentCreate,
    SourceKind,
    TicketCreate,
    TicketEvent,
    TicketKind,
    TicketMigrate,
    TicketRead,
    TicketTransition,
)
from ...config import RepoConfig, ReposRegistry, Settings
from ...config.repos import target_branch_for
from ...stages.merge import _verify_merge_ancestor
from ...core.models import Ticket
from ...core.service import TicketService
from ...core.states import STAGE_FOR_STATE, State
from ...forge import get_forge
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
            _TicketService(settings, board_id=rc.board_id)
            for rc in repos.repos.values()
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

    rc_by_board = {rc.board_id: rc for rc in repos.repos.values()}
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


@router.post("/tickets/{ticket_id}/convert-to-task", status_code=201)
def convert_to_task(
    ticket_id: str,
    request: Request,
    body: dict = Body({}),
    svc=Depends(get_service),
    worker=Depends(get_worker),
    settings=Depends(get_settings),
) -> TicketRead:
    """Draft a new task ticket from an answered inquiry (the ask).

    Feeds the inquiry's question + answer and an optional operator
    ``comment`` to an LLM helper agent, then creates a new ``kind="task"``
    ticket from the agent's drafted title/description on the same board.
    A backlink comment is posted on the source inquiry for traceability.

    404 when the ticket is unknown, 409 when it is not an inquiry, and
    503 when no LLM key is configured.
    """
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")
    if ticket.kind != TicketKind.INQUIRY:
        raise HTTPException(409, "only inquiry tickets can be converted to tasks")

    comment = str(body.get("comment", "") or "")

    ws = svc.workspace(ticket)
    # The answer overwrites description.md; the question is preserved as
    # an artifact. Fall back to the title when the artifact is absent
    # (e.g. inquiry not yet answered).
    answer = ws.read_description()
    question_path = ws.artifacts_dir / "question-original.md"
    if question_path.exists():
        question = question_path.read_text(encoding="utf-8")
    else:
        question = ticket.title

    try:
        result = run_ask_to_ticket_agent(
            settings=settings,
            question=question,
            answer=answer,
            comment=comment,
        )
    except RuntimeError as e:
        raise HTTPException(503, f"ask-to-ticket agent unavailable: {e}") from None

    new_ticket = svc.create(
        result.title,
        result.description,
        source=SourceKind.USER,
        kind=TicketKind.TASK,
        board_id=ticket.board_id or None,
    )
    maybe_enqueue(new_ticket, worker)

    # Backlink on the source inquiry for traceability (best-effort).
    try:
        svc.add_comment(
            ticket_id, f"Converted to task {new_ticket.id}", author="system"
        )
    except Exception:
        log.exception("convert_to_task: failed to post backlink comment")

    repo_config = _repo_config_for_ticket(new_ticket, request.app.state.repos)
    return enrich_ticket_read(new_ticket, settings, svc, repo_config=repo_config)


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


@router.post("/tickets/{ticket_id}/merge-now", response_model=TicketRead)
def merge_now(
    ticket_id: str,
    request: Request,
    svc=Depends(get_service),
    worker=Depends(get_worker),
    settings=Depends(get_settings),
) -> TicketRead:
    """Merge the PR for a ticket in human_mr_approval directly via the
    forge API, then transition to done.  This is the explicit human
    merge path — it bypasses auto-merge eligibility and calls the
    forge's merge endpoint immediately.

    For multi-repo (meta-board) tickets — those whose deliver stage
    wrote ``pr_urls.json`` — this merges the PR of *every* repo listed
    in the manifest, each via that repo's own ``RepoConfig``. Already-
    merged repos are skipped so a re-press after a partial failure is
    idempotent; only when every repo is merged does the ticket advance
    to done.

    Returns 409 when the ticket is not in human_mr_approval, when the
    manifest is corrupt, or when the forge rejects a merge (branch
    protection, conflict, etc.).
    """
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")
    if ticket.state is not State.HUMAN_MR_APPROVAL:
        raise HTTPException(409, "ticket is not in human_mr_approval")

    repo_config = _repo_config_for_ticket(ticket, request.app.state.repos)

    # Multi-repo mode: when the deliver stage wrote ``pr_urls.json`` we
    # merge every touched repo's PR. Reuse the merge stage's helpers so
    # the manifest schema stays single-sourced.
    from ...stages.merge import _load_pr_urls, _repo_config_for_entry

    try:
        pr_entries = _load_pr_urls(svc.workspace(ticket).artifacts_dir)
    except ValueError as e:
        raise HTTPException(409, f"pr_urls.json corrupted: {e}") from e

    if pr_entries:
        merged_urls: list[str] = []
        for entry in pr_entries:
            repo_id = entry.get("repo_id", "")
            branch = entry.get("branch", "")
            url = entry.get("url", branch)
            rc = _repo_config_for_entry(entry)
            entry_forge = get_forge(settings, repo_config=rc)
            # Idempotent re-press: skip repos whose PR is already merged.
            pr = entry_forge.pr_status(source_branch=branch)
            if pr is None or pr.get("merged"):
                merged_urls.append(url)
                continue
            result = entry_forge.merge_pr(source_branch=branch)
            if not result["merged"]:
                raise HTTPException(
                    409, f"merge rejected for {repo_id}: {result['reason']}"
                )
            # Verify the merged commit actually reached the repo's target
            # branch before trusting merge_pr()'s success. A confirmed
            # non-ancestor blocks the DONE transition (best-effort allow
            # when there is no local clone or git errors).
            entry_repo_dir = svc.workspace(ticket).dir / "repos" / repo_id
            repo_dir = (
                str(entry_repo_dir) if (entry_repo_dir / ".git").exists() else None
            )
            sha = pr.get("sha", "")
            target = target_branch_for(settings, rc)
            if not _verify_merge_ancestor(repo_dir, sha, ticket.id, target):
                raise HTTPException(
                    409,
                    f"merge reported success for {repo_id} but commit "
                    f"{sha[:8] or '(none)'} is not on origin/{target} — "
                    "refusing to mark done",
                )
            merged_urls.append(pr.get("url", url))

        ticket = svc.transition(
            ticket_id,
            State.DONE,
            note=f"merged via board: {', '.join(merged_urls)}",
        )
        maybe_enqueue(ticket, worker)  # retrospect picks up DONE
        return enrich_ticket_read(ticket, settings, svc, repo_config=repo_config)

    # Single-repo path (unchanged).
    forge = get_forge(settings, repo_config=repo_config)
    pr = forge.pr_status(source_branch=ticket.branch)
    if pr is None:
        raise HTTPException(409, "no PR found for branch — nothing to merge")
    pr_url = pr.get("url", ticket.branch)

    result = forge.merge_pr(source_branch=ticket.branch)
    if not result["merged"]:
        raise HTTPException(409, result["reason"])

    # Verify the merged commit actually reached origin/<target> before
    # trusting merge_pr()'s success. A confirmed non-ancestor blocks the
    # DONE transition, leaving the ticket in HUMAN_MR_APPROVAL (best-effort
    # allow when there is no local clone or git errors).
    repo = svc.workspace(ticket).repo_dir
    repo_dir = str(repo) if (repo / ".git").exists() else None
    sha = pr.get("sha", "")
    target = target_branch_for(settings, repo_config)
    if not _verify_merge_ancestor(repo_dir, sha, ticket.id, target):
        raise HTTPException(
            409,
            f"merge reported success but commit {sha[:8] or '(none)'} is not "
            f"on origin/{target} — refusing to mark done",
        )

    ticket = svc.transition(
        ticket_id,
        State.DONE,
        note=f"merged via board: {pr_url}",
    )

    maybe_enqueue(ticket, worker)  # retrospect picks up DONE
    return enrich_ticket_read(ticket, settings, svc, repo_config=repo_config)


@router.get("/tickets/{ticket_id}/merge-info")
def get_merge_info(
    ticket_id: str,
    request: Request,
    svc=Depends(get_service),
    settings=Depends(get_settings),
) -> dict:
    """Return CI status, mergeable flag, and changed files for the PR/MR
    backing *ticket_id*.  Each forge call is individually resilient —
    a failure in one field does not crash the whole response."""
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")

    branch = ticket.branch or f"{settings.branch_prefix}{ticket_id}"
    repo_config = _repo_config_for_ticket(ticket, request.app.state.repos)

    # Resolve forge once; remains None when forge is not configured.
    forge = None
    try:
        forge = get_forge(settings, repo_config=repo_config)
    except RuntimeError:
        pass  # forge not configured

    # --- mergeable -------------------------------------------------------
    mergeable: bool | None = None
    if forge is not None:
        try:
            pr = forge.pr_status(source_branch=branch)
            if pr is not None:
                mergeable = pr.get("mergeable")
        except Exception:
            pass

    # --- CI conclusion / failing checks ----------------------------------
    ci_conclusion: str | None = None
    ci_failing: list[dict] = []
    if forge is not None:
        try:
            cs = forge.check_status(source_branch=branch)
            if cs is not None:
                ci_conclusion = cs.get("conclusion")
                if ci_conclusion == "failure":
                    ci_failing = [
                        {
                            "name": f.get("name", ""),
                            "summary": (f.get("summary") or "")[:200],
                        }
                        for f in (cs.get("failing") or [])
                    ]
        except Exception:
            pass

    # --- files -----------------------------------------------------------
    files: list[dict] = []
    if forge is not None:
        try:
            raw = forge.pr_files(source_branch=branch)
            # Sort by total changes desc, cap at 50.
            raw.sort(
                key=lambda f: f.get("additions", 0) + f.get("deletions", 0),
                reverse=True,
            )
            files = raw[:50]
        except Exception:
            pass

    return {
        "mergeable": mergeable,
        "ci_conclusion": ci_conclusion,
        "ci_failing": ci_failing,
        "files": files,
    }


@router.get("/tickets/{ticket_id}/merge-reason")
def get_merge_reason(
    ticket_id: str,
    svc=Depends(get_service),
) -> dict:
    """Return the auto-merge blocking reason written by the merge
    stage, or an empty string when no reason has been recorded."""
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")
    reason_path = svc.workspace(ticket).artifacts_dir / "merge_reason.txt"
    if not reason_path.exists():
        return {"reason": ""}
    return {"reason": reason_path.read_text(encoding="utf-8").strip()}


@router.get("/tickets/{ticket_id}/merge-status")
def get_merge_status(
    ticket_id: str,
    request: Request,
    svc=Depends(get_service),
    settings=Depends(get_settings),
) -> dict:
    """Return live merge-readiness for a ticket's PR.

    Called by the ticket drawer before rendering the Merge button so
    the user sees *why* they can't merge right now (conflicts, failing
    CI, pending checks) instead of hitting a bare 409 from
    ``/merge-now``.  Returns ``can_merge: true`` on transient forge
    errors so the Merge button stays active — the actual merge
    endpoint handles the real rejection.
    """
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")

    # Only relevant for merge-ready states.  Everything else gets a
    # clean "no" so the drawer doesn't bother rendering a button.
    if ticket.state not in (
        State.HUMAN_MR_APPROVAL,
        State.WAITING_AUTO_MERGE,
        State.IMPLEMENT_COMPLETE,
    ):
        return {
            "mergeable": None,
            "ci_conclusion": None,
            "can_merge": False,
            "reason": f"ticket is not in a merge-relevant state (currently {ticket.state.value})",
        }

    repo_config = _repo_config_for_ticket(ticket, request.app.state.repos)
    forge = get_forge(settings, repo_config=repo_config)

    # ── PR mergeability ──────────────────────────────────────────
    mergeable: bool | None = None
    try:
        pr = forge.pr_status(source_branch=ticket.branch)
    except Exception:
        # Transient forge error — stay optimistic; merge-now will
        # surface the real error if the user clicks.
        return {
            "mergeable": None,
            "ci_conclusion": None,
            "can_merge": True,
            "reason": "",
        }

    if pr is None:
        return {
            "mergeable": None,
            "ci_conclusion": None,
            "can_merge": False,
            "reason": "No PR found for this branch",
        }
    mergeable = pr.get("mergeable")

    # ── CI status ────────────────────────────────────────────────
    ci_conclusion: str | None = None
    try:
        ci = forge.check_status(source_branch=ticket.branch)
    except Exception:
        ci = None
    if ci is not None:
        ci_conclusion = ci.get("conclusion")

    # ── Compose result ───────────────────────────────────────────
    if mergeable is False:
        return {
            "mergeable": mergeable,
            "ci_conclusion": ci_conclusion,
            "can_merge": False,
            "reason": "PR has conflicts — rebase needed",
        }
    if ci_conclusion == "failure":
        return {
            "mergeable": mergeable,
            "ci_conclusion": ci_conclusion,
            "can_merge": False,
            "reason": "CI checks are failing",
        }
    if ci_conclusion == "pending":
        return {
            "mergeable": mergeable,
            "ci_conclusion": ci_conclusion,
            "can_merge": False,
            "reason": "CI checks are still running",
        }

    return {
        "mergeable": mergeable,
        "ci_conclusion": ci_conclusion,
        "can_merge": True,
        "reason": "",
    }


@router.post("/tickets/{ticket_id}/request-changes")
def request_changes(
    ticket_id: str,
    body: CommentCreate,
    request: Request,
    svc=Depends(get_service),
    worker=Depends(get_worker),
    settings=Depends(get_settings),
) -> dict:
    """Add a comment AND transition from human_issue_approval back to draft
    in one atomic operation."""
    try:
        comment, ticket = svc.request_changes(ticket_id, body.body, author=body.author)
    except KeyError:
        raise HTTPException(404, "ticket not found") from None
    maybe_enqueue(ticket, worker)
    repo_config = _repo_config_for_ticket(ticket, request.app.state.repos)
    return {
        "comment": comment,
        "ticket": enrich_ticket_read(ticket, settings, svc, repo_config=repo_config),
    }


@router.post("/tickets/{ticket_id}/priority", response_model=TicketRead)
def set_priority(
    ticket_id: str,
    body: dict,
    request: Request,
    svc=Depends(get_service),
    worker=Depends(get_worker),
    settings=Depends(get_settings),
) -> TicketRead:
    """Toggle the operator-controlled priority flag on a ticket.

    Body: ``{"priority": true|false}``.  Re-enqueues the ticket so the
    priority change is reflected in the next consumer pop.
    """
    priority = bool(body.get("priority", False))
    try:
        changed_ids = svc.set_priority(ticket_id, priority)
    except KeyError:
        raise HTTPException(404, "ticket not found") from None
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")
    # Force a fresh enqueue with the new priority rank for every
    # ticket whose priority actually flipped — the target plus any
    # descendants that inherited the flag from an epic. `maybe_enqueue`
    # would short-circuit on the worker's _pending dedup, leaving the
    # stale rank in the heap (see worker.requeue_with_current_priority
    # for the rationale).
    for cid in changed_ids:
        ct = svc.get(cid)
        if ct is not None and ct.state in STAGE_FOR_STATE:
            worker.requeue_with_current_priority(cid)
    repo_config = _repo_config_for_ticket(ticket, request.app.state.repos)
    return enrich_ticket_read(ticket, settings, svc, repo_config=repo_config)


@router.post("/tickets/{ticket_id}/redraft")
def redraft(
    ticket_id: str,
    body: CommentCreate,
    request: Request,
    svc=Depends(get_service),
    worker=Depends(get_worker),
    settings=Depends(get_settings),
) -> dict:
    """Redraft a ticket from any active state back to DRAFT with an
    optional comment."""
    try:
        comment, ticket = svc.redraft(
            ticket_id, body.body or "", author=body.author or "user"
        )
    except KeyError:
        raise HTTPException(404, "ticket not found") from None
    maybe_enqueue(ticket, worker)
    repo_config = _repo_config_for_ticket(ticket, request.app.state.repos)
    return {
        "comment": comment,
        "ticket": enrich_ticket_read(ticket, settings, svc, repo_config=repo_config),
    }


@router.post("/tickets/{ticket_id}/mark-done")
def mark_done(
    ticket_id: str,
    body: dict = Body({}),
    request: Request = None,
    svc=Depends(get_service),
    settings=Depends(get_settings),
) -> TicketRead:
    """Mark a ticket as DONE from any non-terminal state.

    Accepts an optional ``note`` in the JSON body that is recorded
    as the event note.  Returns the updated ticket on success, 404
    when the ticket is unknown, and 409 when the ticket is already in
    a terminal state or an epic.
    """
    try:
        raw_note = body.get("note", "")
        note = str(raw_note) if raw_note else ""
        comment, ticket = svc.mark_done(ticket_id, note=note)
    except KeyError:
        raise HTTPException(404, "ticket not found") from None
    repo_config = _repo_config_for_ticket(ticket, request.app.state.repos)
    return enrich_ticket_read(ticket, settings, svc, repo_config=repo_config)


@router.post("/tickets/{ticket_id}/resume-blocked", response_model=TicketRead)
def resume_blocked(
    ticket_id: str,
    request: Request,
    svc=Depends(get_service),
    worker=Depends(get_worker),
    settings=Depends(get_settings),
) -> TicketRead:
    """Resume a blocked or retrying ticket.

    For BLOCKED tickets, transitions back to the originating state.
    For retrying tickets (retry_attempt > 0 in any non-BLOCKED state),
    clears the retry metadata and re-enqueues immediately.
    """
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")

    if ticket.state is State.BLOCKED:
        try:
            ticket = svc.resume_blocked(ticket_id)
        except KeyError:
            raise HTTPException(404, "ticket not found") from None
    elif ticket.retry_attempt > 0:
        svc.set_retry_state(
            ticket_id,
            retry_attempt=0,
            last_transient_error=None,
            next_retry_at=None,
        )
        ticket = svc.get(ticket_id)
    else:
        raise HTTPException(
            409, f"ticket is not blocked or retrying (currently {ticket.state})"
        )

    maybe_enqueue(ticket, worker)
    repo_config = _repo_config_for_ticket(ticket, request.app.state.repos)
    return enrich_ticket_read(ticket, settings, svc, repo_config=repo_config)


@router.get("/tickets/{ticket_id}/cost-breakdown")
def cost_breakdown(
    ticket_id: str,
    # FastAPI injects Request and ignores the default; the implicit-Optional
    # form is intentional. Suppress the [assignment] error so its PEP-484
    # notes don't trip mypy-baseline's note-block sync.
    request: Request = None,  # type: ignore[assignment]
    svc=Depends(get_service),
    settings=Depends(get_settings),
) -> dict:
    """Per-trace cost breakdown for a ticket, used by the drawer to
    overlay agent-step costs on history rows.

    The Langfuse sessionId is the repo-qualified ticket id
    (``<repo> · <ticket>``, applied inside ``session_traces``), so a
    single `/api/public/traces?sessionId=…` query returns every agent
    invocation tied to the ticket. Each entry carries
    ``{name, cost, at, trace_id}`` ordered by timestamp; the drawer's
    renderHistoryHtml matches the entries to history events by inferred
    agent name + nearest-in-time-≤ pairing.
    """
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")
    repo_config = _repo_config_for_ticket(ticket, request.app.state.repos)
    from ...langfuse.client import session_traces

    rows = session_traces(settings, ticket_id, repo_config=repo_config)
    if rows is None:
        return {"available": False, "traces": []}
    return {"available": True, "traces": rows}
