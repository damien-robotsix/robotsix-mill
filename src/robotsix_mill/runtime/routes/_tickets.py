"""Core ticket lifecycle routes."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ...core.models import (
    CommentCreate,
    TicketCreate,
    TicketEvent,
    TicketRead,
    TicketTransition,
)
from ...core.service import TransitionError
from ...core.states import STAGE_FOR_STATE, State
from ...forge import get_forge
from ..deps import (
    enrich_ticket_read,
    get_service,
    get_settings,
    get_worker,
    maybe_enqueue,
)

log = logging.getLogger(__name__)

router = APIRouter()


def _repo_config_for_ticket(ticket, repos):
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
    repos = request.app.state.repos
    board_id = ""
    if body.repo_id:
        # Explicit repo_id provided — look up its board_id.
        if body.repo_id not in repos.repos:
            sorted_keys = sorted(repos.repos.keys())
            raise HTTPException(
                status_code=400,
                detail=f"Unknown repo: '{body.repo_id}'. Known repos: {sorted_keys}",
            )
        board_id = repos.repos[body.repo_id].board_id
    elif len(repos.repos) == 1:
        # Single-repo mode: default to the sole repo.
        board_id = next(iter(repos.repos.values())).board_id
    else:
        # Multi-repo mode with no repo_id: require it.
        sorted_keys = sorted(repos.repos.keys())
        raise HTTPException(
            status_code=400,
            detail=f"repo_id is required when multiple repos are configured. "
            f"Available repos: {sorted_keys}",
        )

    try:
        ticket = svc.create(
            body.title,
            body.description,
            source=body.source,
            depends_on=body.depends_on,
            kind=body.kind,
            parent_id=body.parent_id,
            board_id=board_id or None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    maybe_enqueue(ticket, worker)  # "directly taken in charge"
    return enrich_ticket_read(ticket, settings, svc)


@router.get("/tickets", response_model=list[TicketRead])
def list_tickets(
    state: State | None = None,
    include_closed: bool = True,
    repo_id: str | None = None,
    request: Request = None,
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

    # With per-repo DBs the default svc only sees its own board's
    # tickets. Build a list of services to query: one per repo when
    # repo_id is omitted or "all", else just the requested repo.
    from ...core.service import TicketService as _TicketService

    repos = request.app.state.repos
    if repo_id and repo_id != "all":
        if repo_id not in repos.repos:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown repo: '{repo_id}'. Known repos: "
                f"{sorted(repos.repos.keys())}",
            )
        services = [_TicketService(settings, board_id=repos.repos[repo_id].board_id)]
    else:
        services = [
            _TicketService(settings, board_id=rc.board_id)
            for rc in repos.repos.values()
        ]

    tickets: list = []
    for s in services:
        try:
            tickets.extend(s.list(state=state, exclude_states=exclude))
        except Exception:
            log.exception("list_tickets: failed to query board %r", s.board_id)

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
    try:
        ticket = svc.transition(ticket_id, body.state, body.note)
    except KeyError:
        raise HTTPException(404, "ticket not found") from None
    except TransitionError as e:
        raise HTTPException(409, str(e)) from None
    maybe_enqueue(ticket, worker)  # human unblock re-triggers the chain
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
    try:
        ticket = svc.transition(ticket_id, State.READY, note="approved by human")
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

    Returns 409 when the ticket is not in human_mr_approval or when
    the forge rejects the merge (branch protection, conflict, etc.).
    """
    ticket = svc.get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")
    if ticket.state is not State.HUMAN_MR_APPROVAL:
        raise HTTPException(409, "ticket is not in human_mr_approval")

    repo_config = _repo_config_for_ticket(ticket, request.app.state.repos)
    forge = get_forge(settings, repo_config=repo_config)
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
            note=f"merged via board: {pr_url}",
        )
    except TransitionError as e:
        raise HTTPException(409, str(e)) from None

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
    except TransitionError as e:
        raise HTTPException(409, str(e)) from None
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
    except TransitionError as e:
        raise HTTPException(409, str(e)) from None
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
    except TransitionError as e:
        raise HTTPException(409, str(e)) from None
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
    repo_config = _repo_config_for_ticket(ticket, request.app.state.repos)
    return enrich_ticket_read(ticket, settings, svc, repo_config=repo_config)
