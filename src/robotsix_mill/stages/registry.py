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
from .document import DocumentStage
from .implement import ImplementStage
from .merge import MergeStage
from .refine import RefineStage
from .retrospect import RetrospectStage
from .review import ReviewStage

_REGISTERED: list[type[Stage]] = [
    RefineStage,
    ImplementStage,
    DocumentStage,
    ReviewStage,
    DeliverStage,
    MergeStage,
    CIFixStage,
    RetrospectStage,
    AnswerStage,
]

STAGES: dict[str, Stage] = {cls.name: cls() for cls in _REGISTERED}


def get_stage(name: str) -> Stage:
    """Retrieve a stage instance by name from the registry.

    Args:
        name: The stage name (e.g., 'refine', 'implement', 'deliver').

    Returns:
        The Stage instance for the given name.

    Raises:
        KeyError: If the stage name is unknown.
    """
    try:
        return STAGES[name]
    except KeyError:
        raise KeyError(f"unknown stage {name!r}; known: {sorted(STAGES)}") from None
