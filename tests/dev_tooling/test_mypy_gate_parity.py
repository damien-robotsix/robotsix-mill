"""Guard against CI / Makefile / pre-commit mypy-gate drift.

All three must call ``scripts/mypy_gate.sh`` (strict mypy filtered through
``mypy-baseline.txt``) so they give the same verdict on the same tree.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_GATE = "scripts/mypy_gate.sh"
_CALLERS = (".github/workflows/ci.yml", "Makefile", ".pre-commit-config.yaml")


def test_gate_script_runs_baseline_filter() -> None:
    script = _REPO_ROOT / _GATE
    text = script.read_text()
    assert (
        "uv run mypy src/ --strict | uv run mypy-baseline filter "
        "--baseline-path mypy-baseline.txt" in text
    )
    # Exit status must come from the filter, as in CI.
    assert "-o pipefail" not in text
    assert os.access(script, os.X_OK)


@pytest.mark.parametrize("rel", _CALLERS)
def test_caller_uses_shared_gate(rel: str) -> None:
    text = (_REPO_ROOT / rel).read_text()
    assert _GATE in text, f"{rel} does not call {_GATE}"
    for line in text.splitlines():
        # Only the baseline-regeneration target may invoke mypy directly.
        if "mypy src/ --strict" in line:
            assert "mypy-baseline sync" in line, f"{rel}: bare mypy: {line!r}"
    assert "mirrors-mypy" not in text
