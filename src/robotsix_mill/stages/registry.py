"""Stage registry: name -> Stage instance.

Adding a stage = importing its class here. The supervisor and CLI look
stages up by name; :data:`STAGES` is the single source of truth.
"""

from __future__ import annotations

from .base import Stage
from .deliver import DeliverStage
from .implement import ImplementStage
from .refine import RefineStage
from .review import ReviewStage

_REGISTERED: list[type[Stage]] = [
    RefineStage,
    ImplementStage,
    ReviewStage,
    DeliverStage,
]

STAGES: dict[str, Stage] = {cls.name: cls() for cls in _REGISTERED}


def get_stage(name: str) -> Stage:
    try:
        return STAGES[name]
    except KeyError:
        raise KeyError(
            f"unknown stage {name!r}; known: {sorted(STAGES)}"
        ) from None
