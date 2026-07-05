"""Health, Langfuse status, repos, gates, board UI routes."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Coroutine
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from sqlmodel import text

if TYPE_CHECKING:
    from ..worker import Worker

from ...config import Settings
from ...core import db
from ...core.states import State
from ..board_adapter import MillBoardAdapter
from ..board_html import build_board_skeleton, render_board_html
from ...config import RepoConfig
from ..deps import enrich_ticket_read, get_repos_registry, get_settings, get_worker
from ...langfuse.client import _build_read_client, _langfuse_api_get

log = logging.getLogger(__name__)

router = APIRouter(tags=["Health"])


def _uptime_payload(request: Request) -> dict[str, Any]:
    """Return a dict with ``started_at`` / ``uptime_seconds`` when the
    app-state ``started_at`` is set, empty dict otherwise."""
    started_at: datetime | None = getattr(request.app.state, "started_at", None)
    if started_at is not None:
        uptime = (datetime.now(timezone.utc) - started_at).total_seconds()
        return {
            "started_at": started_at.isoformat(),
            "uptime_seconds": int(uptime),
        }
    return {}


@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": "alive"}
    payload.update(_uptime_payload(request))
    return payload


def _resolve_board_id(request: Request) -> str | None:
    """Return a board_id suitable for a health-check DB probe.

    When ``single_repo_id`` is set on app state use its board_id;
    otherwise pick the first registered repo's board_id.  Returns
    ``None`` when the repos registry is empty.
    """
    repos = request.app.state.repos
    single: str | None = request.app.state.single_repo_id
    if single is not None:
        rc = repos.repos[single]
        return str(rc.repo_id)
    if not repos.repos:
        return None
    return str(next(iter(repos.repos.values())).repo_id)


async def _check_database(settings: Settings, board_id: str) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        with db.get_engine(settings, board_id).connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        log.warning("health /ready: database check failed: %s", exc)
        elapsed = int((time.perf_counter() - start) * 1000)
        return {"name": "database", "status": "error", "latency_ms": elapsed}
    else:
        elapsed = int((time.perf_counter() - start) * 1000)
        return {"name": "database", "status": "ok", "latency_ms": elapsed}


async def _check_langfuse(settings: Settings) -> dict[str, Any]:
    start = time.perf_counter()
    client = await asyncio.to_thread(_build_read_client, settings)
    if client is None:
        return {"name": "langfuse", "status": "skipped", "latency_ms": 0}
    try:
        result = await asyncio.to_thread(
            _langfuse_api_get, settings, "/api/public/traces", {"limit": 1}
        )
    except Exception as exc:
        log.warning("health /ready: langfuse check failed: %s", exc)
        elapsed = int((time.perf_counter() - start) * 1000)
        return {"name": "langfuse", "status": "error", "latency_ms": elapsed}
    else:
        elapsed = int((time.perf_counter() - start) * 1000)
        if result is None:
            return {"name": "langfuse", "status": "error", "latency_ms": elapsed}
        return {"name": "langfuse", "status": "ok", "latency_ms": elapsed}


@router.get("/health/ready", response_model=None)
async def health_ready(
    request: Request, settings: Settings = Depends(get_settings)
) -> JSONResponse | dict[str, Any]:
    board_id = _resolve_board_id(request)

    async def _with_timeout(
        coro: Coroutine[Any, Any, dict[str, Any] | None],
    ) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(coro, timeout=2.0)
        except asyncio.TimeoutError:
            return None  # caller must replace with a timeout result

    async def _db_check() -> dict[str, Any]:
        if board_id is None:
            return {"name": "database", "status": "skipped", "latency_ms": 0}
        return await _check_database(settings, board_id)

    checks: list[dict[str, Any]] = []

    # DB check
    db_result = await _with_timeout(_db_check())
    if db_result is None:
        checks.append({"name": "database", "status": "timeout", "latency_ms": 2000})
    else:
        checks.append(db_result)

    # Langfuse check
    lf_result = await _with_timeout(_check_langfuse(settings))
    if lf_result is None:
        checks.append({"name": "langfuse", "status": "timeout", "latency_ms": 2000})
    else:
        checks.append(lf_result)

    any_down = any(c["status"] in {"error", "timeout"} for c in checks)
    body: dict[str, Any] = {
        "status": "ready" if not any_down else "not_ready",
        "checks": checks,
    }
    if any_down:
        return JSONResponse(status_code=503, content=body)
    return body


@router.get("/langfuse-status")
def langfuse_status() -> dict:
    """Return recent Langfuse export failures so the UI can surface
    "tracing broken" without the operator having to grep worker logs.

    Empty ``failures`` list means everything is shipping fine.
    """
    from ..tracing import get_export_failures

    failures = get_export_failures()
    return {"failures": failures, "count": len(failures)}


@router.get("/credit-status")
def credit_status() -> dict[str, object]:
    """Return the current low-OpenRouter-credit warning state.

    Polled by the board UI's ``fetchCreditStatus()`` every refresh
    cycle.  ``low`` is ``true`` when the balance is below the
    configured threshold OR a 402 insufficient-credit error was seen.
    """
    from ..credit_status import get_credit_status

    return get_credit_status()


@router.post(
    "/credit-status/clear",
    status_code=204,
)
def credit_status_clear() -> None:
    """Dismiss the low-credit warning after the operator acknowledges it."""
    from ..credit_status import clear_credit_status

    clear_credit_status()


@router.get("/worker-status")
def worker_status(worker: "Worker" = Depends(get_worker)) -> dict[str, object]:
    """Live worker introspection for diagnosing stuck tickets.

    Reports per-board queue depth, the in-flight ``_pending`` set, and
    consumer-task health (incl. the exception of any task that died — a
    dead per-board consumer is why a ``ready`` ticket on that board would
    never be popped). Read-only.
    """
    tasks = list(getattr(worker, "_tasks", []))
    dead: list[dict[str, str]] = []
    for t in tasks:
        if t.done() and not t.cancelled():
            exc = t.exception()
            if exc is not None:
                dead.append(
                    {"repr": repr(t), "exception": f"{type(exc).__name__}: {exc}"}
                )
    poll = getattr(worker, "_poll_task", None)
    return {
        "queues": {bid: q.qsize() for bid, q in worker.queues.items()},
        "pending": sorted(worker._pending),
        "tasks_total": len(tasks),
        "tasks_alive": sum(1 for t in tasks if not t.done()),
        "tasks_done": sum(1 for t in tasks if t.done()),
        "dead_tasks": dead,
        "poll_task_alive": (poll is not None and not poll.done()),
    }


@router.post("/langfuse-status/clear", status_code=204)
def langfuse_status_clear() -> None:
    """Drop the failure log after the operator acknowledges."""
    from ..tracing import clear_export_failures

    clear_export_failures()


def _public_forge_url(url: str | None) -> str | None:
    """Strip any userinfo (tokens) from a forge remote URL before exposing it."""
    if not url:
        return None
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    if parts.hostname is None:
        return url  # not a URL shape (e.g. ssh scp-like) — return as-is
    netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@router.get("/repos")
def list_repos(
    request: Request,
    repos=Depends(get_repos_registry),
) -> list[dict]:
    """Return the registered repos for the UI repo selector.

    No secrets (Langfuse keys) are included — ``repo_id``, ``board_id``
    and a credential-stripped ``forge_remote_url`` (so agent consumers
    like robotsix-chat can locate the code).  In single-repo mode
    (``--repo-id`` passed) only that repo is returned.
    """

    def _entry(rc: "RepoConfig") -> dict[str, str | None]:
        return {
            "repo_id": rc.repo_id,
            "board_id": rc.repo_id,
            "forge_remote_url": _public_forge_url(rc.forge_remote_url),
        }

    single = request.app.state.single_repo_id
    if single is not None:
        return [_entry(repos.repos[single])]
    result = [_entry(rc) for rc in repos.repos.values()]
    # The cross-repo meta-agent files extraction proposals to a synthetic
    # "meta" board that is NOT a registered repo (no clone/forge — see
    # meta/runner.py, board_id="meta"). Surface it in the selector so
    # operators can review those drafts; it is deliberately kept out of
    # the ReposRegistry so the worker/clone/cost loops never touch it.
    result.append({"repo_id": "meta", "board_id": "meta", "forge_remote_url": None})
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

    from robotsix_board import render_config_script

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
    exclude = (
        set() if show_closed else {State.CLOSED, State.EPIC_CLOSED, State.ANSWERED}
    )
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
