"""Health, Langfuse status, repos, gates, board UI routes."""

from __future__ import annotations

import json as _json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ..board_html import BOARD_HTML
from ...core.states import State
from ..deps import get_repos_registry, get_settings

log = logging.getLogger(__name__)

router = APIRouter()


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
        {"repo_id": rc.repo_id, "board_id": rc.board_id}
        for rc in repos.repos.values()
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
