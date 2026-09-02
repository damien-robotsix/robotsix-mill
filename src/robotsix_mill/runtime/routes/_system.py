"""System-level operational controls.

Drain mode ("update-pending"): an API flag the caretaker sets when it
sees a pending image update.  While armed, the worker stops STARTING new
heavy stages (implement / ci_fix / refine) and lets in-flight ones
finish, so the mill drains to a quiet, deployable point.  The flag is
in-memory only and carries a TTL (fail-open), so a stale flag whose
caretaker has disappeared — or a process restart — resumes normal intake
automatically.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..deps import get_worker
from ..worker import Worker
from ..worker.core import DEFAULT_DRAIN_TTL_SECONDS

router = APIRouter(tags=["System"])


class DrainRequest(BaseModel):
    """Body for ``POST /system/drain``.

    ``ttl_seconds`` (optional) overrides the fail-open TTL.  ``enabled``
    lets a caller clear drain mode immediately without waiting for the
    TTL or a restart.
    """

    enabled: bool = True
    ttl_seconds: int | None = Field(
        default=None, ge=1, description="Override TTL; default is the worker default."
    )


@router.get("/system/drain")
def get_drain(worker: Worker = Depends(get_worker)) -> dict[str, Any]:
    """Report current drain status (armed, drained, expiry, in-flight stages)."""
    return worker.drain_status()


@router.post("/system/drain")
def set_drain(
    body: DrainRequest,
    worker: Worker = Depends(get_worker),
) -> dict[str, Any]:
    """Arm (or clear) drain mode.

    The caretaker calls this with the default ``enabled=true`` when it
    sees a pending update, polls ``GET /system/drain`` (or ``/health``)
    until ``drained`` is true, deploys, and clears the flag on restart
    (in-memory state is lost).  Pass ``enabled=false`` to clear before
    the TTL expires.
    """
    if body.enabled:
        worker.set_drain(ttl_seconds=body.ttl_seconds or DEFAULT_DRAIN_TTL_SECONDS)
    else:
        worker.clear_drain()
    return worker.drain_status()
