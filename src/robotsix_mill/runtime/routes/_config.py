"""Component config HTTP surface — implements the config-ownership standard.

Provides ``GET /config``, ``PUT /config``, ``GET /config/versions``,
and ``POST /config/rollback``.  Secret keys are masked on read and
rejected on write — they remain env-injected by the deploy plane.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from robotsix_mill.config import Settings
from robotsix_mill.runtime.deps import get_settings
from robotsix_mill.runtime.config_service import (
    ConfigValidationError,
    get_config,
    get_versions,
    rollback_config,
    update_config,
)

router = APIRouter(tags=["Config"])


@router.get("/config")
def config_get(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """Return the effective config with secrets masked and the JSON Schema."""
    result: dict[str, Any] = get_config(settings)
    return result


@router.put("/config", response_model=None)
def config_put(
    updates: dict[str, Any],
    request: Request,
    settings: Settings = Depends(get_settings),
) -> JSONResponse | dict[str, Any]:
    """Apply a partial config update.  Secrets are rejected."""
    try:
        result: dict[str, Any] = update_config(updates, data_dir=settings.data_dir)
        return result
    except ConfigValidationError as exc:
        return JSONResponse(
            status_code=422,
            content={
                "type": "urn:robotsix:error:config-validation",
                "title": "Config validation failed",
                "detail": str(exc),
                "instance": str(request.url.path),
            },
        )


@router.get("/config/versions")
def config_versions(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """Return recent config versions with timestamps and changed keys."""
    result: dict[str, Any] = get_versions(settings.data_dir)
    return result


@router.post("/config/rollback", response_model=None)
def config_rollback(
    body: dict[str, Any],
    request: Request,
    settings: Settings = Depends(get_settings),
) -> JSONResponse | dict[str, Any]:
    """Rollback to a previous config version.  Creates a new version."""
    target_version = body.get("version")
    if not isinstance(target_version, int):
        return JSONResponse(
            status_code=422,
            content={
                "type": "urn:robotsix:error:config-validation",
                "title": "Config validation failed",
                "detail": "'version' must be an integer",
                "instance": str(request.url.path),
            },
        )
    try:
        result: dict[str, Any] = rollback_config(
            target_version, data_dir=settings.data_dir
        )
        return result
    except ConfigValidationError as exc:
        return JSONResponse(
            status_code=422,
            content={
                "type": "urn:robotsix:error:config-validation",
                "title": "Config validation failed",
                "detail": str(exc),
                "instance": str(request.url.path),
            },
        )
