"""Route domain modules — each owns its own ``APIRouter``.

The parent ``router`` aggregates all child routers via
``include_router`` so ``api.py`` can continue to do
``from . import routes; app.include_router(routes.router)`` unchanged.
"""

from __future__ import annotations

from fastapi import APIRouter

from . import (
    _agents,
    _board,
    _candidates,
    _chat_skill,
    _comments,
    _config,
    _diagnostic_events,
    _epics,
    _health,
    _passes,
    _repos,
    _step_usage,
    _system,
    _tickets,
    _tickets_ingest,
    _tickets_merge,
    _tickets_transitions,
    _traces,
)

router = APIRouter()

router.include_router(_health.router)
router.include_router(_comments.router)
router.include_router(_tickets.router)
router.include_router(_tickets_merge.router)
router.include_router(_tickets_transitions.router)
router.include_router(_epics.router)
router.include_router(_passes.router)
router.include_router(_traces.router)
router.include_router(_candidates.router)
router.include_router(_agents.router)
router.include_router(_board.router)
router.include_router(_chat_skill.router)
router.include_router(_diagnostic_events.router)
router.include_router(_repos.router)
router.include_router(_step_usage.router)
router.include_router(_tickets_ingest.router)
router.include_router(_config.router)
router.include_router(_system.router)
