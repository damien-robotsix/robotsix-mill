"""Route domain modules — each owns its own ``APIRouter``.

The parent ``router`` aggregates all child routers via
``include_router`` so ``api.py`` can continue to do
``from . import routes; app.include_router(routes.router)`` unchanged.
"""

from __future__ import annotations

from fastapi import APIRouter

from . import _health
from . import _comments
from . import _tickets
from . import _tickets_merge
from . import _tickets_transitions
from . import _epics
from . import _passes
from . import _traces
from . import _candidates
from . import _agents
from . import _board
from . import _repos
from . import _tickets_ingest

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
router.include_router(_repos.router)
router.include_router(_tickets_ingest.router)
