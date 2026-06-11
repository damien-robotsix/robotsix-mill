"""Health, Langfuse status, repos, gates, board UI routes."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from ..board_adapter import MillBoardAdapter
from ..board_html import build_board_skeleton, render_board_html
from ...core.states import State
from ..deps import enrich_ticket_read, get_repos_registry, get_settings

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
def health(request: Request) -> dict:
    started_at: datetime | None = getattr(request.app.state, "started_at", None)
    if started_at is not None:
        uptime = (datetime.now(timezone.utc) - started_at).total_seconds()
        return {
            "status": "ok",
            "started_at": started_at.isoformat(),
            "uptime_seconds": int(uptime),
        }
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
    result = [
        {"repo_id": rc.repo_id, "board_id": rc.board_id} for rc in repos.repos.values()
    ]
    # The cross-repo meta-agent files extraction proposals to a synthetic
    # "meta" board that is NOT a registered repo (no clone/forge — see
    # meta/runner.py, board_id="meta"). Surface it in the selector so
    # operators can review those drafts; it is deliberately kept out of
    # the ReposRegistry so the worker/clone/cost loops never touch it.
    result.append({"repo_id": "meta", "board_id": "meta"})
    return result


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
        "comments_after_body": settings.comments_after_body,
    }


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def board() -> str:
    adapter = MillBoardAdapter()
    # board.js (JSON_HYDRATION) only diffs cards into existing columns,
    # so the column skeleton is rendered server-side here.
    skeleton = build_board_skeleton(adapter.columns())

    try:
        from robotsix_board import render_config_script
    except ImportError:
        # robotsix-board not installed yet — serve the shell without
        # the board config script; the board will be empty until the
        # dependency is available.
        return render_board_html("", skeleton)

    config_script = render_config_script(
        adapter,
        refresh_url="/board/cards",
        refresh_interval_ms=5_000,
    )
    return render_board_html(config_script, skeleton)


@router.websocket("/ws/board")
async def ws_board(websocket: WebSocket) -> None:
    """WebSocket endpoint for real-time board updates (live auto-refresh).

    On connect, sends the full ticket list as the first message; subsequent
    messages are ``ticket_update`` events pushed by the broadcaster on each
    ticket transition.

    Deps are read straight off ``websocket.app.state`` rather than via
    ``Depends(...)``: FastAPI cannot resolve HTTP ``Request``-based
    dependencies for a WebSocket scope, so declaring them made the handshake
    fail with **403 before ``accept()``**. (This route also previously lived
    only in the unincluded legacy ``_misc.py``, so it was never registered —
    which silently disabled the board's live auto-refresh.)
    """
    await websocket.accept()

    svc = websocket.app.state.service
    settings = websocket.app.state.settings
    broadcaster = websocket.app.state.broadcaster

    show_closed = websocket.query_params.get("show_closed", "").lower() == "true"
    exclude = set() if show_closed else {State.CLOSED, State.EPIC_CLOSED}
    tickets = svc.list(exclude_states=exclude)
    initial = [
        enrich_ticket_read(
            t, settings, svc, blocking_cost=False, fetch_pr_url=False
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
    except Exception:  # noqa: BLE001 — never let a push error kill the socket
        pass
    finally:
        broadcaster.unsubscribe(q)
