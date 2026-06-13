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

This module is a thin façade (Pattern A) over the ``implement`` package:

- ``_shared`` — module-level constants/regexes, stateless helpers, the
  internal dataclasses, and the package ``log`` (the pure leaf).
- ``phase_coordinator`` — :class:`PhaseCoordinatorMixin` (run / loop /
  context load / finalize / pause).
- ``validation`` — :class:`ValidationMixin` (prerequisite gate, baseline
  check, scope guardrail).
- ``implementation_logic`` — :class:`ImplementationLogicMixin` (agent
  invocation, single pass, test/result evaluation).
- ``file_operations`` — :class:`FileOperationsMixin` (clone/branch,
  repo-change and gitignore checks).
- ``core`` — :class:`ImplementStage` (the assembled ``Stage`` subclass).

The patchable seams ``run_test_agent`` / ``run_smoke_agent`` /
``load_repo_smoke_paths`` are bound as package attributes so the
existing test monkeypatches (``robotsix_mill.stages.implement.run_test_agent``
and ``setattr(impl_mod, "run_smoke_agent"/"load_repo_smoke_paths", ...)``)
intercept the real call sites, which reach them through a
``from robotsix_mill.stages import implement as _facade`` indirection.
"""

from __future__ import annotations

from ...agents.testing import run_smoke_agent, run_test_agent
from ...config.repo_settings import load_repo_smoke_paths
from ._shared import _FLOOD_SAMPLE_SIZE, _modules_yaml_added_paths
from .core import ImplementStage

__all__ = [
    "ImplementStage",
    # patchable seams (re-exported so the `_facade.<name>` call sites and
    # the `robotsix_mill.stages.implement.<name>` monkeypatch targets
    # resolve to the same callables).
    "run_test_agent",
    "run_smoke_agent",
    "load_repo_smoke_paths",
    # internal helpers imported directly by the unit tests.
    "_FLOOD_SAMPLE_SIZE",
    "_modules_yaml_added_paths",
]
