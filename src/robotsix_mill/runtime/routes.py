"""HTTP route handlers for the robotsix-mill management-plane API.

All endpoints are registered on a module-level ``APIRouter`` named
``router``.  Handlers use ``fastapi.Depends`` to obtain the service,
worker, and settings that were stored on ``app.state`` during lifespan
startup, replacing the closure-based helpers that were previously
defined inside ``create_app()``.
"""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse

from ..core.models import (
    Comment,
    CommentCreate,
    TicketCreate,
    TicketEvent,
    TicketRead,
    TicketTransition,
)
from ..core.service import TransitionError
from ..core.states import State
from .board_html import BOARD_HTML
from .deps import (
    enrich_ticket_read,
    get_run_registry,
    get_service,
    get_settings,
    get_worker,
    maybe_enqueue,
)

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def board() -> str:
    return BOARD_HTML


@router.post("/tickets", response_model=TicketRead, status_code=201)
def create_ticket(
    body: TicketCreate,
    svc=Depends(get_service),
    worker=Depends(get_worker),
    settings=Depends(get_settings),
) -> TicketRead:
    ticket = svc.create(body.title, body.description, depends_on=body.depends_on)
    maybe_enqueue(ticket, worker)  # "directly taken in charge"
    return enrich_ticket_read(ticket, settings, svc)


@router.get("/tickets", response_model=list[TicketRead])
def list_tickets(
    state: State | None = None,
    include_closed: bool = True,
    svc=Depends(get_service),
    settings=Depends(get_settings),
) -> list[TicketRead]:
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
    # include_closed=false hides CLOSED (the volume case) but keeps
    # DONE visible — DONE is the transient retrospect-in-flight window
    # and we want to watch retrospect work without toggling.
    exclude = None
    if not include_closed:
        exclude = {State.CLOSED}
    return [
        enrich_ticket_read(
            t, settings, svc, blocking_cost=False, fetch_pr_url=False
        )
        for t in svc.list(state=state, exclude_states=exclude)
    ]


@router.get("/tickets/{ticket_id}", response_model=TicketRead)
def get_ticket(
    ticket_id: str,
    svc=Depends(get_service),
    settings=Depends(get_settings),
) -> TicketRead:
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")
    return enrich_ticket_read(ticket, settings, svc)


@router.get("/tickets/{ticket_id}/history", response_model=list[TicketEvent])
def get_history(
    ticket_id: str,
    svc=Depends(get_service),
) -> list[TicketEvent]:
    if svc.get(ticket_id) is None:
        raise HTTPException(404, "ticket not found")
    return svc.history(ticket_id)


@router.get("/tickets/{ticket_id}/description")
def get_description(
    ticket_id: str,
    svc=Depends(get_service),
) -> dict:
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")
    return {"description": svc.workspace(ticket).read_description()}


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
    svc=Depends(get_service),
    worker=Depends(get_worker),
    settings=Depends(get_settings),
) -> TicketRead:
    try:
        ticket = svc.transition(ticket_id, body.state, body.note)
    except KeyError:
        raise HTTPException(404, "ticket not found") from None
    except TransitionError as e:
        raise HTTPException(409, str(e)) from None
    maybe_enqueue(ticket, worker)  # human unblock re-triggers the chain
    return enrich_ticket_read(ticket, settings, svc)


@router.post("/tickets/{ticket_id}/approve", response_model=TicketRead)
def approve_ticket(
    ticket_id: str,
    svc=Depends(get_service),
    worker=Depends(get_worker),
    settings=Depends(get_settings),
) -> TicketRead:
    try:
        ticket = svc.transition(
            ticket_id, State.READY, note="approved by human"
        )
    except KeyError:
        raise HTTPException(404, "ticket not found") from None
    except TransitionError as e:
        raise HTTPException(409, str(e)) from None
    maybe_enqueue(ticket, worker)  # implement picks it up from ready
    return enrich_ticket_read(ticket, settings, svc)


@router.post(
    "/tickets/{ticket_id}/comments",
    response_model=Comment,
    status_code=201,
)
def add_comment(
    ticket_id: str,
    body: CommentCreate,
    svc=Depends(get_service),
) -> Comment:
    """Add a comment to a ticket (any state). Does NOT change state."""
    try:
        return svc.add_comment(ticket_id, body.body)
    except KeyError:
        raise HTTPException(404, "ticket not found") from None


@router.get(
    "/tickets/{ticket_id}/comments",
    response_model=list[Comment],
)
def list_comments(
    ticket_id: str,
    svc=Depends(get_service),
) -> list[Comment]:
    """List all comments for a ticket, ordered oldest-first."""
    try:
        return svc.list_comments(ticket_id)
    except KeyError:
        raise HTTPException(404, "ticket not found") from None


@router.post("/tickets/{ticket_id}/request-changes")
def request_changes(
    ticket_id: str,
    body: CommentCreate,
    svc=Depends(get_service),
    worker=Depends(get_worker),
    settings=Depends(get_settings),
) -> dict:
    """Add a comment AND transition from awaiting_approval back to draft
    in one atomic operation."""
    try:
        comment, ticket = svc.request_changes(ticket_id, body.body)
    except KeyError:
        raise HTTPException(404, "ticket not found") from None
    except TransitionError as e:
        raise HTTPException(409, str(e)) from None
    maybe_enqueue(ticket, worker)
    return {"comment": comment, "ticket": enrich_ticket_read(ticket, settings, svc)}


@router.post("/tickets/{ticket_id}/resume-blocked", response_model=TicketRead)
def resume_blocked(
    ticket_id: str,
    svc=Depends(get_service),
    worker=Depends(get_worker),
    settings=Depends(get_settings),
) -> TicketRead:
    """Resume a blocked ticket back to the state it was blocked from."""
    try:
        ticket = svc.resume_blocked(ticket_id)
    except KeyError:
        raise HTTPException(404, "ticket not found") from None
    except TransitionError as e:
        raise HTTPException(409, str(e)) from None
    maybe_enqueue(ticket, worker)
    return enrich_ticket_read(ticket, settings, svc)


@router.post("/audit", status_code=202)
def audit_pass(
    registry=Depends(get_run_registry),
) -> dict:
    """Kick off an audit pass in the BACKGROUND and return at once.

    The audit runs the LLM agent for minutes — blocking the HTTP
    response made the browser fetch drop ("NetworkError"). New draft
    tickets appear on the board when it finishes.
    """
    from ..audit_runner import run_audit_pass

    run_id = registry.start("audit")

    def _run() -> None:
        try:
            r = run_audit_pass()
            draft_ids = [d["id"] for d in r.drafts_created[:5]]
            summary = (
                f"Created {len(r.drafts_created)} drafts: "
                f"{', '.join(draft_ids)}"
                f"{'…' if len(r.drafts_created) > 5 else ''}"
            )
            registry.finish_ok(run_id, summary)
            log.info(
                "audit pass done: %d draft(s)", len(r.drafts_created)
            )
        except Exception as e:  # noqa: BLE001 — background; just log
            log.exception("audit pass failed")
            registry.finish_error(run_id, str(e))

    threading.Thread(
        target=_run, name="audit-pass", daemon=True
    ).start()
    return {"status": "started"}


@router.post("/scout", status_code=202)
def scout_pass(
    registry=Depends(get_run_registry),
) -> dict:
    """Trigger a scout pass in the BACKGROUND: reads memory, evaluates
    OpenRouter models, writes updated memory, creates draft tickets for
    model improvements.
    """
    from ..scout_runner import run_scout_pass

    run_id = registry.start("scout")

    def _run() -> None:
        try:
            result = run_scout_pass()
            draft_ids = [d["id"] for d in result.drafts_created[:5]]
            summary = (
                f"Created {len(result.drafts_created)} drafts: "
                f"{', '.join(draft_ids)}"
                f"{'…' if len(result.drafts_created) > 5 else ''}"
            )
            registry.finish_ok(run_id, summary)
            log.info(
                "scout pass done: %d draft(s)",
                len(result.drafts_created),
            )
        except Exception as e:  # noqa: BLE001 — background; just log
            log.exception("scout pass failed")
            registry.finish_error(run_id, str(e))

    threading.Thread(
        target=_run, name="scout-pass", daemon=True
    ).start()
    return {"status": "started"}


@router.post("/trace-health", status_code=202)
def trace_health_check(
    registry=Depends(get_run_registry),
) -> dict:
    """Kick off a trace-health check in the BACKGROUND and return at
    once.  The check fetches Langfuse traces from the last 24h,
    detects unsessioned traces, and files a draft ticket if needed.
    No LLM — deterministic and fast.
    """
    from ..trace_health_runner import run_trace_health_check

    run_id = registry.start("trace-health")

    def _run() -> None:
        try:
            r = run_trace_health_check()
            summary = (
                f"{r.unsessioned_count}/{r.total_traces} "
                f"traces unsessioned ({r.window_start} to "
                f"{r.window_end}) — "
                f"{'draft created' if r.draft_created else 'no alert'}"
            )
            registry.finish_ok(run_id, summary)
            if r.draft_created:
                log.info(
                    "trace-health check: draft created — "
                    "%d/%d traces unsessioned",
                    r.unsessioned_count,
                    r.total_traces,
                )
            else:
                log.info(
                    "trace-health check: no alert "
                    "(%d/%d traces unsessioned)",
                    r.unsessioned_count,
                    r.total_traces,
                )
        except Exception as e:  # noqa: BLE001 — background; just log
            log.exception("trace-health check failed")
            registry.finish_error(run_id, str(e))

    threading.Thread(
        target=_run, name="trace-health-check", daemon=True
    ).start()
    return {"status": "started"}


@router.get("/runs")
def list_runs(
    registry=Depends(get_run_registry),
) -> list[dict]:
    """Return recent background-run entries (newest first)."""
    return registry.list_all()


@router.post("/health-check", status_code=202)
def health_check_pass(
    registry=Depends(get_run_registry),
) -> dict:
    """Kick off a codebase-health pass in the BACKGROUND and return at
    once.

    The health pass runs the LLM agent for minutes — blocking the HTTP
    response made the browser fetch drop ("NetworkError"). New draft
    tickets appear on the board when it finishes.

    Mirrors the audit/scout/trace-health pattern: registers the run on
    start so the /runs panel shows it in-flight, and on finish so it
    flips to ok/error with a summary. Without this the run is silently
    happening behind the scenes — the Langfuse trace exists but the
    board reports nothing.
    """
    from ..health_runner import run_health_pass

    run_id = registry.start("health")

    def _run() -> None:
        try:
            r = run_health_pass()
            draft_ids = [d["id"] for d in r.drafts_created[:5]]
            summary = (
                f"Created {len(r.drafts_created)} drafts: "
                f"{', '.join(draft_ids)}"
                f"{'…' if len(r.drafts_created) > 5 else ''}"
            )
            registry.finish_ok(run_id, summary)
            log.info(
                "health pass done: %d draft(s)", len(r.drafts_created)
            )
        except Exception as e:  # noqa: BLE001 — background; just log
            log.exception("health pass failed")
            registry.finish_error(run_id, str(e))

    threading.Thread(
        target=_run, name="health-pass", daemon=True
    ).start()
    return {"status": "started"}
