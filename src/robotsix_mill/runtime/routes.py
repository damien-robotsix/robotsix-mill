"""HTTP route handlers for the robotsix-mill management-plane API.

All endpoints are registered on a module-level ``APIRouter`` named
``router``.  Handlers use ``fastapi.Depends`` to obtain the service,
worker, and settings that were stored on ``app.state`` during lifespan
startup, replacing the closure-based helpers that were previously
defined inside ``create_app()``.
"""

from __future__ import annotations

import json as _json
import logging
import threading

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..core.models import (
    Comment,
    CommentCreate,
    TicketCreate,
    TicketEvent,
    TicketRead,
    TicketTransition,
)
from ..config import get_secrets
from ..core.service import TransitionError
from ..core.states import State
from ..forge import get_forge
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


@router.get("/gates")
def gates(settings=Depends(get_settings)) -> dict:
    """Return the four pipeline gate flags from the live configuration.

    Same open access model as ``/health`` — no auth.  The board polls
    these every refresh cycle and renders them as header pills so the
    operator always sees which behavioural gates are active.
    """
    return {
        "auto_approve": settings.auto_approve_enabled,
        "review": settings.review_enabled,
        "auto_merge": settings.auto_merge_enabled,
        "require_approval": settings.require_approval,
    }


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def board() -> str:
    st_json = _json.dumps([s.value for s in State])
    return BOARD_HTML.replace("{ST_STATES}", st_json)


@router.post("/tickets", response_model=TicketRead, status_code=201)
def create_ticket(
    body: TicketCreate,
    svc=Depends(get_service),
    worker=Depends(get_worker),
    settings=Depends(get_settings),
) -> TicketRead:
    try:
        ticket = svc.create(
            body.title,
            body.description,
            source=body.source,
            depends_on=body.depends_on,
            kind=body.kind,
            parent_id=body.parent_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
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
    # include_closed=false hides CLOSED and EPIC_CLOSED (the volume
    # cases) but keeps DONE visible — DONE is the transient
    # retrospect-in-flight window and we want to watch retrospect work
    # without toggling.
    exclude = None
    if not include_closed:
        exclude = {State.CLOSED, State.EPIC_CLOSED}
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

    # If this ticket has an epic parent, check for a proposed epic body
    # artifact and apply it to the epic on approval.
    try:
        if ticket.parent_id:
            parent = svc.get(ticket.parent_id)
            if parent is not None and parent.kind == "epic":
                artifact = (
                    svc.workspace(ticket).artifacts_dir
                    / "epic-body-proposed.md"
                )
                if artifact.exists():
                    epic_body = artifact.read_text(encoding="utf-8").strip()
                    if epic_body:
                        new_hash = svc.workspace(parent).write_description(
                            epic_body
                        )
                        svc.set_content_hash(parent.id, new_hash)
    except Exception:
        pass  # best-effort: approval always succeeds

    maybe_enqueue(ticket, worker)  # implement picks it up from ready
    return enrich_ticket_read(ticket, settings, svc)


@router.post("/tickets/{ticket_id}/approve-mr", response_model=TicketRead)
def approve_mr(
    ticket_id: str,
    svc=Depends(get_service),
    worker=Depends(get_worker),
    settings=Depends(get_settings),
) -> TicketRead:
    """Human approves a ticket in human_mr_approval — merge the PR now.

    Semantically identical to ``/merge-now``: the human is the
    approval, so call the forge's merge endpoint directly instead of
    transitioning to waiting_auto_merge (which would re-check the
    reviewer's ``auto_merge_eligible`` flag and bounce back to
    human_mr_approval whenever the reviewer set it false). The
    docstring promised "bypasses auto-merge eligibility checks"; the
    old implementation didn't.
    """
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")
    if ticket.state is not State.HUMAN_MR_APPROVAL:
        raise HTTPException(409, "ticket is not in human_mr_approval")

    forge = get_forge(settings)
    pr = forge.pr_status(source_branch=ticket.branch)
    if pr is None:
        raise HTTPException(409, "no PR found for branch — nothing to merge")
    pr_url = pr.get("url", ticket.branch)

    result = forge.merge_pr(source_branch=ticket.branch)
    if not result["merged"]:
        raise HTTPException(409, result["reason"])

    try:
        ticket = svc.transition(
            ticket_id,
            State.DONE,
            note=f"approved + merged by human: {pr_url}",
        )
    except TransitionError as e:
        raise HTTPException(409, str(e)) from None

    return enrich_ticket_read(ticket, settings, svc)


@router.post("/tickets/{ticket_id}/merge-now", response_model=TicketRead)
def merge_now(
    ticket_id: str,
    svc=Depends(get_service),
    worker=Depends(get_worker),
    settings=Depends(get_settings),
) -> TicketRead:
    """Merge the PR for a ticket in human_mr_approval directly via the
    forge API, then transition to done.  This is the explicit human
    merge path — it bypasses auto-merge eligibility and calls the
    forge's merge endpoint immediately.

    Returns 409 when the ticket is not in human_mr_approval or when
    the forge rejects the merge (branch protection, conflict, etc.).
    """
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")
    if ticket.state is not State.HUMAN_MR_APPROVAL:
        raise HTTPException(
            409, "ticket is not in human_mr_approval"
        )

    forge = get_forge(settings)
    pr = forge.pr_status(source_branch=ticket.branch)
    if pr is None:
        raise HTTPException(
            409, "no PR found for branch — nothing to merge"
        )
    pr_url = pr.get("url", ticket.branch)

    result = forge.merge_pr(source_branch=ticket.branch)
    if not result["merged"]:
        raise HTTPException(409, result["reason"])

    try:
        ticket = svc.transition(
            ticket_id,
            State.DONE,
            note=f"merged via board: {pr_url}",
        )
    except TransitionError as e:
        raise HTTPException(409, str(e)) from None

    maybe_enqueue(ticket, worker)  # retrospect picks up DONE
    return enrich_ticket_read(ticket, settings, svc)


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
    reason_path = (
        svc.workspace(ticket).artifacts_dir / "merge_reason.txt"
    )
    if not reason_path.exists():
        return {"reason": ""}
    return {"reason": reason_path.read_text(encoding="utf-8").strip()}


@router.post(
    "/tickets/{ticket_id}/comments",
    response_model=Comment,
    status_code=201,
)
def add_comment(
    ticket_id: str,
    body: CommentCreate,
    svc=Depends(get_service),
    settings=Depends(get_settings),
) -> Comment:
    """Add a comment to a ticket (any state).

    Set *parent_id* to reply to an existing comment, forming a
    threaded discussion.  Omit it (or pass ``null``) to start a new
    top-level thread.

    For epic tickets, the comment triggers a background re-processing:
    the epic is re-broken-down by the breakdown agent with the full
    comment history as operator direction, and net-new children are
    created.  Non-epic tickets are unaffected — the comment is simply
    persisted.
    """
    try:
        comment = svc.add_comment(ticket_id, body.body, author=body.author, parent_id=body.parent_id)
    except KeyError:
        raise HTTPException(404, "ticket not found") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None

    # Fire-and-forget: re-process the epic in a daemon thread.
    ticket = svc.get(ticket_id)
    if ticket is not None and ticket.kind == "epic":
        from .worker import _run_epic_reprocess

        threading.Thread(
            target=_run_epic_reprocess,
            args=(ticket_id, body.body, settings),
            daemon=True,
        ).start()

    return comment


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


@router.post("/comments/{comment_id}/close", response_model=Comment)
def close_thread(
    comment_id: int,
    svc=Depends(get_service),
) -> Comment:
    """Close a top-level comment thread to mark it as resolved."""
    try:
        return svc.close_thread(comment_id)
    except KeyError:
        raise HTTPException(404, "comment not found") from None
    except ValueError as e:
        raise HTTPException(409, str(e)) from None


@router.post("/comments/{comment_id}/reopen", response_model=Comment)
def reopen_thread(
    comment_id: int,
    svc=Depends(get_service),
) -> Comment:
    """Reopen a previously-closed comment thread."""
    try:
        return svc.reopen_thread(comment_id)
    except KeyError:
        raise HTTPException(404, "comment not found") from None
    except ValueError as e:
        raise HTTPException(409, str(e)) from None


@router.post("/tickets/{ticket_id}/request-changes")
def request_changes(
    ticket_id: str,
    body: CommentCreate,
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
    except TransitionError as e:
        raise HTTPException(409, str(e)) from None
    maybe_enqueue(ticket, worker)
    return {"comment": comment, "ticket": enrich_ticket_read(ticket, settings, svc)}


@router.post("/tickets/{ticket_id}/redraft")
def redraft(
    ticket_id: str,
    body: CommentCreate,
    svc=Depends(get_service),
    worker=Depends(get_worker),
    settings=Depends(get_settings),
) -> dict:
    """Redraft a ticket from any active state back to DRAFT with an
    optional comment."""
    try:
        comment, ticket = svc.redraft(ticket_id, body.body or "", author=body.author or "user")
    except KeyError:
        raise HTTPException(404, "ticket not found") from None
    except TransitionError as e:
        raise HTTPException(409, str(e)) from None
    maybe_enqueue(ticket, worker)
    return {"comment": comment, "ticket": enrich_ticket_read(ticket, settings, svc)}


@router.post("/tickets/{ticket_id}/mark-done")
def mark_done(
    ticket_id: str,
    body: dict = Body({}),
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
    except TransitionError as e:
        raise HTTPException(409, str(e)) from None
    return enrich_ticket_read(ticket, settings, svc)


@router.post("/epics", response_model=TicketRead, status_code=201)
def create_epic(
    body: dict,
    svc=Depends(get_service),
    settings=Depends(get_settings),
) -> TicketRead:
    """Create a new epic — accepts ``{"title": str, "description": str}``."""
    title = body.get("title", "")
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    description = body.get("description", "")
    try:
        ticket = svc.create(title=title, description=description, kind="epic")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return enrich_ticket_read(ticket, settings, svc)


@router.get(
    "/tickets/{ticket_id}/children",
    response_model=list[TicketRead],
)
def list_children(
    ticket_id: str,
    svc=Depends(get_service),
    settings=Depends(get_settings),
) -> list[TicketRead]:
    """Return all tickets whose ``parent_id`` equals *ticket_id*."""
    if svc.get(ticket_id) is None:
        raise HTTPException(404, "ticket not found")
    return [
        enrich_ticket_read(t, settings, svc, blocking_cost=False, fetch_pr_url=False)
        for t in svc.list_children(ticket_id)
    ]


@router.post("/tickets/{ticket_id}/generate-children", status_code=202)
def generate_children(
    ticket_id: str,
    svc=Depends(get_service),
    settings=Depends(get_settings),
    registry=Depends(get_run_registry),
) -> dict:
    """Generate child tickets from an epic description using the LLM
    epic-breakdown agent.  Returns ``202 Accepted`` immediately — the
    agent runs in a background thread.

    Returns ``400`` if the ticket is not an epic.  Returns ``404`` if
    the ticket does not exist.
    """
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")
    if ticket.kind != "epic":
        raise HTTPException(400, "ticket is not an epic")

    run_id = registry.start("epic-breakdown")

    def _run() -> None:
        try:
            from ..agents.epic_breakdown import run_epic_breakdown_agent

            description = svc.workspace(ticket).read_description()
            result = run_epic_breakdown_agent(
                settings=settings,
                epic_title=ticket.title,
                epic_description=description,
            )
            created_ids: list[str] = []
            for title, body in zip(result.child_titles, result.child_bodies):
                child = svc.create(
                    title=title,
                    description=body,
                    kind="task",
                    parent_id=ticket_id,
                )
                created_ids.append(child.id)

            # Build linear dependency chain: C0 ← C1 ← C2 ← ...
            for i in range(1, len(created_ids)):
                svc.set_depends_on(created_ids[i], [created_ids[i - 1]])

            # Apply the revised epic body to the epic immediately
            # (generate-children is a one-shot manual trigger).
            if result.epic_body and result.epic_body.strip():
                new_hash = svc.workspace(ticket).write_description(
                    result.epic_body.strip()
                )
                svc.set_content_hash(ticket_id, new_hash)

            summary = (
                f"Created {len(created_ids)} children: "
                f"{', '.join(created_ids[:5])}"
                f"{'…' if len(created_ids) > 5 else ''}"
            )
            registry.finish_ok(run_id, summary)
            log.info(
                "epic-breakdown done: %d children for %s",
                len(created_ids), ticket_id,
            )
        except Exception as e:  # noqa: BLE001 — background; just log
            log.exception("epic-breakdown failed for %s", ticket_id)
            registry.finish_error(run_id, str(e))

    threading.Thread(
        target=_run, name=f"epic-breakdown-{ticket_id}", daemon=True
    ).start()
    return {"status": "started"}


@router.post("/tickets/{ticket_id}/resume-blocked", response_model=TicketRead)
def resume_blocked(
    ticket_id: str,
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
        except TransitionError as e:
            raise HTTPException(409, str(e)) from None
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


@router.post("/bc-check", status_code=202)
def bc_check_pass(
    registry=Depends(get_run_registry),
) -> dict:
    """Kick off a bc-check pass in the BACKGROUND and return at once.

    The bc-check agent inspects the codebase for backward-compat shims
    and dead-code branches that are ripe for removal, drafting tickets
    when it finds candidates. New drafts appear on the board when it
    finishes.
    """
    from ..bc_check_runner import run_bc_check_pass

    run_id = registry.start("bc-check")

    def _run() -> None:
        try:
            r = run_bc_check_pass()
            draft_ids = [d["id"] for d in r.drafts_created[:5]]
            summary = (
                f"Created {len(r.drafts_created)} drafts: "
                f"{', '.join(draft_ids)}"
                f"{'…' if len(r.drafts_created) > 5 else ''}"
            )
            registry.finish_ok(run_id, summary)
            log.info(
                "bc-check pass done: %d draft(s)", len(r.drafts_created)
            )
        except Exception as e:  # noqa: BLE001 — background; just log
            log.exception("bc-check pass failed")
            registry.finish_error(run_id, str(e))

    threading.Thread(
        target=_run, name="bc-check-pass", daemon=True
    ).start()
    return {"status": "started"}


@router.post("/completeness-check", status_code=202)
def completeness_check_pass(
    registry=Depends(get_run_registry),
) -> dict:
    from ..completeness_check_runner import run_completeness_check_pass
    run_id = registry.start("completeness-check")

    def _run() -> None:
        try:
            r = run_completeness_check_pass()
            draft_ids = [d["id"] for d in r.drafts_created[:5]]
            summary = (
                f"Created {len(r.drafts_created)} drafts: "
                f"{', '.join(draft_ids)}"
                f"{'…' if len(r.drafts_created) > 5 else ''}"
            )
            registry.finish_ok(run_id, summary)
            log.info("completeness-check pass done: %d draft(s)", len(r.drafts_created))
        except Exception as e:
            log.exception("completeness-check pass failed")
            registry.finish_error(run_id, str(e))

    threading.Thread(target=_run, name="completeness-check-pass", daemon=True).start()
    return {"status": "started"}


@router.post("/agent-check", status_code=202)
def agent_check_pass(
    registry=Depends(get_run_registry),
) -> dict:
    """Kick off an agent-check pass in the BACKGROUND and return at
    once. The agent inspects every agent's prompt, tools, and
    structured output, looking for coherence gaps (e.g. an agent
    promising behaviour its tools can't deliver). New draft tickets
    appear on the board when it finishes.
    """
    from ..agent_check_runner import run_agent_check_pass

    run_id = registry.start("agent_check")

    def _run() -> None:
        try:
            r = run_agent_check_pass()
            draft_ids = [d["id"] for d in r.drafts_created[:5]]
            summary = (
                f"Created {len(r.drafts_created)} drafts: "
                f"{', '.join(draft_ids)}"
                f"{'…' if len(r.drafts_created) > 5 else ''}"
            )
            registry.finish_ok(run_id, summary)
            log.info(
                "agent-check pass done: %d draft(s)",
                len(r.drafts_created),
            )
        except Exception as e:  # noqa: BLE001 — background; just log
            log.exception("agent-check pass failed")
            registry.finish_error(run_id, str(e))

    threading.Thread(
        target=_run, name="agent-check-pass", daemon=True
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


@router.get("/active")
def list_active(worker=Depends(get_worker)) -> list[dict]:
    """Return tickets currently being processed by a pipeline stage."""
    return [
        {"ticket_id": tid, "stage": info["stage"], "started_at": info["started_at"]}
        for tid, info in worker._active.items()
    ]


@router.get("/costs/trend")
def cost_trend(
    lookback_hours: float = 24,
    settings=Depends(get_settings),
) -> dict:
    """Return cost bucketed by time for the sparkline chart.

    ``?lookback_hours=N`` is clamped to [1, 168].

    Response shape: ``{"buckets": [{"ts": "...", "total_cost": ..., "trace_count": ...}, ...]}``.
    """
    from ..langfuse_client import aggregate_cost_trend

    lookback_hours = max(1.0, min(lookback_hours, 168.0))
    buckets = aggregate_cost_trend(settings, lookback_hours)
    return {"buckets": buckets}


@router.get("/costs/by-agent")
def cost_by_agent(
    lookback_hours: float = 24,
    settings=Depends(get_settings),
) -> list[dict]:
    """Return cost aggregated by agent/stage name for recent Langfuse
    traces within *lookback_hours* (clamped 1–168)."""
    from ..langfuse_client import aggregate_cost_by_name

    lookback_hours = max(1.0, min(lookback_hours, 168.0))
    return aggregate_cost_by_name(settings, lookback_hours)


@router.get("/costs/most-expensive-ticket")
def most_expensive_ticket_endpoint(
    lookback_hours: float = 24,
    settings=Depends(get_settings),
    svc=Depends(get_service),
):
    """Return the ticket with the highest total LLM cost in the last
    *lookback_hours* (clamped 1–168).  Returns ``null`` when there is
    no data, tracing is disabled, or the session has no matching ticket
    in the database."""
    from ..langfuse_client import most_expensive_ticket

    lookback_hours = max(1.0, min(lookback_hours, 168.0))
    result = most_expensive_ticket(settings, lookback_hours)
    if result is None:
        return None

    session_id = result["session_id"]
    ticket = svc.get(session_id)
    if ticket is None:
        return None

    return {
        "ticket_id": ticket.id,
        "title": ticket.title,
        "cost_usd": result["total_cost"],
    }


@router.get("/costs/most-expensive-trace")
def most_expensive_trace_endpoint(
    lookback_hours: float = 24,
    settings=Depends(get_settings),
):
    """Return the single most expensive trace in the last
    *lookback_hours* (clamped 1–168).  Returns ``null`` when there is
    no data or tracing is disabled."""
    from ..langfuse_client import most_expensive_trace

    lookback_hours = max(1.0, min(lookback_hours, 168.0))
    return most_expensive_trace(settings, lookback_hours)


# -- deep-review --------------------------------------------------------


@router.get("/traces/recent")
def list_recent_traces(
    limit: int = 10,
    min_cost: float | None = None,
    max_cost: float | None = None,
    settings=Depends(get_settings),
) -> list[dict]:
    """Return recent Langfuse traces, filtered by cost and limited in
    count.  *limit* is clamped to 1–50; *min_cost* and *max_cost* are
    inclusive USD filters on ``totalCost``."""
    from ..langfuse_client import list_recent_traces as _list_recent

    limit = max(1, min(limit, 50))
    traces = _list_recent(
        settings,
        limit=limit,
        min_cost=min_cost,
        max_cost=max_cost,
    )
    return [
        {
            "id": t.get("id", ""),
            "name": t.get("name", ""),
            "timestamp": t.get("timestamp", ""),
            "sessionId": t.get("sessionId"),
            "totalCost": t.get("totalCost"),
            "userId": t.get("userId"),
        }
        for t in traces
    ]


@router.post("/traces/{trace_id}/deep-review", status_code=202)
def deep_review_trace(
    trace_id: str,
    request: Request,
    settings=Depends(get_settings),
    registry=Depends(get_run_registry),
) -> dict:
    """Start a background deep review of a single Langfuse trace."""
    if not settings.tracing_enabled:
        return {"status": "unavailable"}

    state = request.app.state
    if not hasattr(state, "deep_review_results"):
        state.deep_review_results = {}

    from ..langfuse_client import fetch_trace_detail
    from ..agents.trace_inspector import run_trace_inspector
    from . import tracing

    run_id = registry.start("deep-review")

    def _run() -> None:
        try:
            detail = fetch_trace_detail(settings, trace_id)
            if detail is None:
                data = {
                    "status": "error",
                    "error": "trace unavailable — could not fetch from Langfuse",
                    "findings": [],
                    "source_trace_name": "(unnamed)",
                }
                state.deep_review_results[trace_id] = data
                state.deep_review_store.put(trace_id, data)
                registry.finish_error(
                    run_id, f"deep review of trace {trace_id}: trace unavailable"
                )
                return

            import json as _json
            import subprocess
            from ..vcs import git_ops

            # Clone the forge repo so the inspector can read_file /
            # list_dir / explore the actual code that produced this
            # trace. Best-effort: if the clone fails (no forge
            # configured, network down) we still run the inspector
            # in tool-less mode. The clone is at a stable, reusable
            # path; later passes reuse it.
            repo_dir = None
            if settings.forge_remote_url:
                cand = settings.data_dir / "deep_review_workspace" / "repo"
                try:
                    if (cand / ".git").exists():
                        # Update the existing clone in place.
                        try:
                            git_ops.try_rebase_onto(cand, settings.forge_target_branch)
                        except Exception:  # noqa: BLE001 — best effort
                            pass
                        repo_dir = cand
                    else:
                        git_ops.clone(
                            settings.forge_remote_url, cand,
                            settings.forge_target_branch, get_secrets().forge_token,
                        )
                        repo_dir = cand
                except subprocess.CalledProcessError as e:
                    log.warning(
                        "deep review clone failed (running tool-less): %s",
                        (e.stderr or "")[:200],
                    )

            # Read inspector memory (best-effort).
            memory_file = settings.trace_inspector_memory_file
            memory = ""
            if memory_file.exists():
                try:
                    memory = memory_file.read_text(encoding="utf-8")
                except OSError:
                    memory = ""

            trace_data = _json.dumps(detail, default=str)
            # Wrap the LLM call in an OTel root span so its pydantic-ai
            # spans get exported as a properly-named, session-grouped
            # Langfuse trace.
            with tracing.start_ticket_root_span(
                tracing.make_session_id("deep-review"), "deep-review",
                extra_attributes={"source_trace_id": trace_id},
            ):
                result = run_trace_inspector(
                    settings=settings,
                    trace_data=trace_data,
                    repo_dir=repo_dir,
                    memory=memory,
                )
            # Persist updated memory verbatim (atomic write).
            if result.updated_memory:
                try:
                    memory_file.parent.mkdir(parents=True, exist_ok=True)
                    tmp = memory_file.with_suffix(".md.tmp")
                    tmp.write_text(result.updated_memory, encoding="utf-8")
                    tmp.replace(memory_file)
                except OSError as e:
                    log.warning(
                        "deep review: could not write memory file: %s", e
                    )

            data = {
                # JS renderDeepReviewResult treats status=="error" as
                # "show the error message" — use it for inspector
                # failures too so the UI surfaces the cause instead of
                # rendering an indistinguishable all-zeros result.
                "status": "ok" if not result.error else "error",
                "trace_id": trace_id,
                "findings": [f.model_dump() for f in result.findings],
                "error": result.error,
            }
            data["source_trace_name"] = detail.get("name", "(unnamed)")
            state.deep_review_results[trace_id] = data
            state.deep_review_store.put(trace_id, data)

            n_findings = len(result.findings)
            n_te = sum(1 for f in result.findings if f.category == "tool_error")
            n_al = sum(1 for f in result.findings if f.category == "agent_limitation")
            n_opt = sum(1 for f in result.findings if f.category == "optimization")
            if result.error:
                summary = f"deep review of trace {trace_id}: {result.error[:120]}"
                registry.finish_error(run_id, result.error[:300])
            else:
                summary = (
                    f"deep review of trace {trace_id}: "
                    f"{n_findings} findings ({n_te} TE, {n_al} AL, {n_opt} OPT)"
                )
                registry.finish_ok(run_id, summary)
            log.info("deep review of trace %s complete", trace_id)
        except Exception as e:  # noqa: BLE001 — background; just log
            log.exception("deep review of trace %s failed", trace_id)
            data = {
                "status": "error",
                "error": str(e),
                "findings": [],
                "source_trace_name": "(unnamed)",
            }
            state.deep_review_results[trace_id] = data
            state.deep_review_store.put(trace_id, data)
            registry.finish_error(run_id, str(e))

    # Mark as running before thread starts.
    state.deep_review_results[trace_id] = {"status": "running"}
    threading.Thread(
        target=_run, name=f"deep-review-{trace_id}", daemon=True
    ).start()
    return {"status": "started", "trace_id": trace_id}


@router.get("/deep-review/{trace_id}")
def get_deep_review_result(
    trace_id: str,
    request: Request,
) -> dict:
    """Return the stored deep-review result for *trace_id*."""
    state = request.app.state
    # Check in-memory first (catches running + recently completed).
    results = getattr(state, "deep_review_results", None)
    if results and trace_id in results:
        entry = results[trace_id]
        if isinstance(entry, dict) and entry.get("status") == "running":
            return entry
        return entry
    # Fall back to disk store.
    store = getattr(state, "deep_review_store", None)
    if store is not None:
        entry = store.get(trace_id)
        if entry is not None:
            return entry
    raise HTTPException(404, "no review found for this trace")


@router.get("/deep-review")
def list_deep_reviews(request: Request) -> list[dict]:
    """Return all stored deep reviews, newest first. Empty list if none."""
    store = getattr(request.app.state, "deep_review_store", None)
    if store is None:
        return []
    return store.list_all()


@router.post("/health-check", status_code=202)
def health_check_pass(
    registry=Depends(get_run_registry),
) -> dict:
    """Kick off a codebase-health pass in the BACKGROUND and return at
    once.

    The health pass runs the LLM agent for minutes — blocking the HTTP
    response made the browser fetch drop ("NetworkError"). New draft
    tickets appear on the board when it finishes.

    Mirrors the audit/trace-health pattern: registers the run on
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


@router.post("/test-gap", status_code=202)
def test_gap_pass(
    registry=Depends(get_run_registry),
) -> dict:
    """Kick off a test-gap inspection pass in the BACKGROUND."""
    from ..test_gap_runner import run_test_gap_pass

    run_id = registry.start("test-gap")

    def _run() -> None:
        try:
            r = run_test_gap_pass()
            draft_ids = [d["id"] for d in r.drafts_created[:5]]
            summary = (
                f"Created {len(r.drafts_created)} drafts: "
                f"{', '.join(draft_ids)}"
                f"{'…' if len(r.drafts_created) > 5 else ''}"
            )
            registry.finish_ok(run_id, summary)
            log.info(
                "test-gap pass done: %d draft(s)", len(r.drafts_created)
            )
        except Exception as e:  # noqa: BLE001 — background; just log
            log.exception("test-gap pass failed")
            registry.finish_error(run_id, str(e))

    threading.Thread(
        target=_run, name="test-gap-pass", daemon=True
    ).start()
    return {"status": "started"}


@router.post("/survey", status_code=202)
def survey_pass(
    registry=Depends(get_run_registry),
) -> dict:
    """Kick off a survey pass in the BACKGROUND and return at once.

    The survey agent discovers similar open-source projects, studies
    their approaches, and proposes concrete improvements as draft
    tickets. New drafts appear on the board when it finishes.
    """
    from ..survey_runner import run_survey_pass

    run_id = registry.start("survey")

    def _run() -> None:
        try:
            r = run_survey_pass()
            draft_ids = [d["id"] for d in r.drafts_created[:5]]
            summary = (
                f"Created {len(r.drafts_created)} drafts: "
                f"{', '.join(draft_ids)}"
                f"{'…' if len(r.drafts_created) > 5 else ''}"
            )
            registry.finish_ok(run_id, summary)
            log.info(
                "survey pass done: %d draft(s)", len(r.drafts_created)
            )
        except Exception as e:  # noqa: BLE001 — background; just log
            log.exception("survey pass failed")
            registry.finish_error(run_id, str(e))

    threading.Thread(
        target=_run, name="survey-pass", daemon=True
    ).start()
    return {"status": "started"}


@router.post("/env-sync", status_code=202)
def env_sync_pass(
    registry=Depends(get_run_registry),
) -> dict:
    """Kick off an env-sync drift detection pass in the BACKGROUND."""
    from ..env_sync_runner import run_env_sync_pass

    run_id = registry.start("env-sync")

    def _run() -> None:
        try:
            r = run_env_sync_pass()
            draft_ids = [d["id"] for d in r.drafts_created[:5]]
            summary = (
                f"Created {len(r.drafts_created)} drafts: "
                f"{', '.join(draft_ids)}"
                f"{'…' if len(r.drafts_created) > 5 else ''}"
            )
            registry.finish_ok(run_id, summary)
            log.info(
                "env-sync pass done: %d draft(s)", len(r.drafts_created)
            )
        except Exception as e:
            log.exception("env-sync pass failed")
            registry.finish_error(run_id, str(e))

    threading.Thread(
        target=_run, name="env-sync-pass", daemon=True
    ).start()
    return {"status": "started"}
