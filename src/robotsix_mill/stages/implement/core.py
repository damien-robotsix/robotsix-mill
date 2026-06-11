"""The :class:`ImplementStage` coordinator.

Assembles the responsibility-focused mixins
(:class:`~.phase_coordinator.PhaseCoordinatorMixin`,
:class:`~.validation.ValidationMixin`,
:class:`~.implementation_logic.ImplementationLogicMixin`,
:class:`~.file_operations.FileOperationsMixin`) into the public
``Stage`` subclass via multiple inheritance.

This module is the only one in the package that imports the mixins; the
mixins never import each other or ``core`` (cross-responsibility calls
go through ``cls``/``self`` on the assembled class), so the package
import graph is a strict acyclic DAG.
"""

from __future__ import annotations

from ...core.states import State
from ..base import Stage
from .file_operations import FileOperationsMixin
from .implementation_logic import ImplementationLogicMixin
from .phase_coordinator import PhaseCoordinatorMixin
from .validation import ValidationMixin


class ImplementStage(
    PhaseCoordinatorMixin,
    ValidationMixin,
    ImplementationLogicMixin,
    FileOperationsMixin,
    Stage,
):
    """Clone the repo, create a feature branch, and run the implementation agent loop to produce code changes."""

    name = "implement"
    input_state = State.READY
