"""Miscellaneous route handlers — health, status, board, runs, active,
WebSocket board push."""

from __future__ import annotations

import json as _json

from fastapi import Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from ...core.states import State
from ..board_html import BOARD_HTML
from ..deps import (
    enrich_ticket_read,
    get_broadcaster,
    get_repos_registry,
    get_run_registry,
    get_service,
    get_settings,
    get_worker,
)
from . import router


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/langfuse-status")
def langfuse_status() -> dict:
    """Return recent Langfuse export failures so the UI can surface
    "tracing broken" without the operator having to grep worker logs.

    Empty ``failures`` list means everything is shipping fine.
    """
    from ..tracing import get_export_failures

    failures = get_export_failures()
    return {"failures": failures, "count": len(failures)}


@router.post("/langfuse-status/clear", status_code=204)
def langfuse_status_clear() -> None:
    """Drop the failure log after the operator acknowledges."""
    from ..tracing import clear_export_failures

    clear_export_failures()


@router.get("/repos")
def list_repos(
    request: Request,
    repos=Depends(get_repos_registry),
) -> list[dict]:
    """Return the registered repos for the UI repo selector.

    No secrets (Langfuse keys) are included — only ``repo_id`` and
    ``board_id``.  In single-repo mode (``--repo-id`` passed) only
    that repo is returned.
    """
    single = request.app.state.single_repo_id
    if single is not None:
        rc = repos.repos[single]
        return [{"repo_id": rc.repo_id, "board_id": rc.board_id}]
    return [
        {"repo_id": rc.repo_id, "board_id": rc.board_id} for rc in repos.repos.values()
    ]


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


@router.get("/runs")
def list_runs(
    repo_id: str | None = None,
    request: Request = None,
    registry=Depends(get_run_registry),
) -> list[dict]:
    """Return recent background-run entries (newest first).

    ``?repo_id=X`` filters to runs associated with that repo.
    When omitted, returns all (current behaviour preserved).
    """
    entries = registry.list_all()
    if repo_id is not None:
        repos = request.app.state.repos
        if repo_id == "all":
            pass  # no filtering
        elif repo_id not in repos.repos:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown repo: '{repo_id}'. Known repos: "
                f"{sorted(repos.repos.keys())}",
            )
        else:
            # Filter entries that carry a repo_id matching the request.
            # Empty repo_id is treated as "applies to any repo" — covers
            # legacy entries filed before per-repo tagging landed plus
            # global runs from periodic agents that don't carry a
            # repo_id today. Strict equality on a non-empty filter would
            # hide every pre-wiring run in single-repo deployments.
            entries = [
                e
                for e in entries
                if e.get("repo_id") == repo_id or not e.get("repo_id")
            ]
    return entries


@router.get("/active")
def list_active(
    repo_id: str | None = None,
    request: Request = None,
    worker=Depends(get_worker),
) -> list[dict]:
    """Return tickets currently being processed by a pipeline stage.

    ``?repo_id=X`` filters to active tickets belonging to that repo.
    When omitted, returns all (current behaviour preserved).
    """
    active = [
        {"ticket_id": tid, "stage": info["stage"], "started_at": info["started_at"]}
        for tid, info in worker._active.items()
    ]
    if repo_id is not None:
        repos = request.app.state.repos
        if repo_id == "all":
            pass  # no filtering
        elif repo_id not in repos.repos:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown repo: '{repo_id}'. Known repos: "
                f"{sorted(repos.repos.keys())}",
            )
        else:
            target_board = repos.repos[repo_id].board_id
            # Look up each active ticket's board_id from the service
            filtered = []
            for item in active:
                ticket = worker.ctx.service.get(item["ticket_id"])
                if ticket and ticket.board_id == target_board:
                    filtered.append(item)
            active = filtered
    return active


# -- WebSocket board push --------------------------------------------------


@router.websocket("/ws/board")
async def ws_board(
    websocket: WebSocket,
    request: Request,
    svc=Depends(get_service),
    settings=Depends(get_settings),
    broadcaster=Depends(get_broadcaster),
):
    """WebSocket endpoint for real-time board updates.

    On connect, sends the full ticket list as the first message so the
    board doesn't need an initial HTTP fetch.  Subsequent messages are
    ``ticket_update`` events pushed by the broadcaster whenever a
    ticket state transition occurs.
    """
    await websocket.accept()

    # Honour the board's showClosed toggle via query param.
    show_closed = request.query_params.get("show_closed", "").lower() == "true"
    exclude = set() if show_closed else {State.CLOSED, State.EPIC_CLOSED}
    tickets = svc.list(exclude_states=exclude)
    initial = [
        enrich_ticket_read(
            t, settings, svc, blocking_cost=False, fetch_pr_url=False,
        ).model_dump(mode="json")
        for t in tickets
    ]

    q = await broadcaster.subscribe(initial)

    try:
        while True:
            data = await q.get()
            await websocket.send_text(data)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        broadcaster.unsubscribe(q)
