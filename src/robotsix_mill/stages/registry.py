"""Stage registry: name -> Stage instance.

Adding a stage = importing its class here. :data:`STAGES` is the single
source of truth; it must cover every value in
:data:`robotsix_mill.core.states.STAGE_FOR_STATE`.
"""

from __future__ import annotations

from .answer import AnswerStage
from .base import Stage
from .ci_fix import CIFixStage
from .deliver import DeliverStage
from .implement import ImplementStage
from .merge import MergeStage
from .refine import RefineStage
from .retrospect import RetrospectStage
from .review import ReviewStage

_REGISTERED: list[type[Stage]] = [
    RefineStage,
    ImplementStage,
    ReviewStage,
    DeliverStage,
    MergeStage,
    CIFixStage,
    RetrospectStage,
    AnswerStage,
]

STAGES: dict[str, Stage] = {cls.name: cls() for cls in _REGISTERED}


def get_stage(name: str) -> Stage:
    try:
        return STAGES[name]
    except KeyError:
        raise KeyError(
            f"unknown stage {name!r}; known: {sorted(STAGES)}"
        ) from None
