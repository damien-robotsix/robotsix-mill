"""Implement stage: READY -> DELIVERABLE (or BLOCKED, resumable).

First run: clone the target repo into the ticket workspace, branch,
then run a deterministic, stage-owned fix loop: invoke the implement
agent for one edit pass, run the test gate, and — on failure — re-invoke
the agent with a distilled diagnosis. The routing (proceed / retry /
escalate) is decided in Python (see
:class:`~..agents.coordinating.ValidationResult`), bounded by
``settings.max_fix_iterations``. Pass -> DELIVERABLE.

Resume: if the ticket workspace already has the clone + its branch (a
prior BLOCKED run), do NOT re-clone — check the branch out and continue
from the committed WIP.

Everything that isn't success is BLOCKED-resumable with WIP committed:
no remote, clone failure, no changes, sandbox down, agent error/budget
cap, or tests still failing after ``max_fix_iterations``. Pushing the
branch + opening the MR happens later, in the deliver stage.

This module is a thin façade (Pattern A) over the ``implement`` package:

- ``file_operations`` — module constants/dataclasses + binary-artifact
  helpers and :class:`FileOperationsMixin`
- ``implementation_logic`` — :class:`ImplementationLogicMixin` (agent
  coordination + prerequisite/baseline gates)
- ``validation`` — :class:`ValidationMixin` (scope guardrail + test-result
  evaluation, the two largest methods)
- ``phase_coordinator`` — :class:`ImplementStage` (the assembled ``Stage``
  subclass + multi-phase orchestration)

The test-gate seams (``run_smoke_agent``, ``run_test_agent``,
``load_repo_smoke_paths``) are bound as package attributes so the existing
``impl_mod.<name>`` monkeypatch targets resolve to the same objects the
submodules call through (via a deferred ``import`` of this package).
"""

from __future__ import annotations

from ...agents.testing import run_smoke_agent, run_test_agent
from ...repo_settings import load_repo_smoke_paths
from . import file_operations as _file_operations
from . import implementation_logic as _implementation_logic
from . import validation as _validation
from .file_operations import BINARY_ARTIFACT_EXTENSIONS
from .phase_coordinator import ImplementStage

# Bind the assembled class onto the mixin submodules so their staticmethods
# that call ``ImplementStage.<helper>`` across submodules resolve the name at
# call time (a bare global in a submodule would otherwise be undefined).
setattr(_file_operations, "ImplementStage", ImplementStage)
setattr(_implementation_logic, "ImplementStage", ImplementStage)
setattr(_validation, "ImplementStage", ImplementStage)

__all__ = [
    "ImplementStage",
    "BINARY_ARTIFACT_EXTENSIONS",
    # test-gate seams (re-exported so ``impl_mod.<name>`` patches resolve)
    "run_smoke_agent",
    "run_test_agent",
    "load_repo_smoke_paths",
]
