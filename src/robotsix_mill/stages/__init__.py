"""Pipeline stages: Stage/StageContext/Outcome contract, STAGES registry, and six stages (implement, refine, ci_fix, merge, deliver, retrospect)."""

from .base import Outcome, Stage, StageContext, stage_context_for
from .registry import STAGES, get_stage

__all__ = [
    "Stage",
    "StageContext",
    "Outcome",
    "STAGES",
    "get_stage",
    "stage_context_for",
]
