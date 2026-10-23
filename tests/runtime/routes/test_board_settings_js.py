"""Drive the board-mill.js Settings-panel dismissal tests from pytest.

``board_settings_harness.mjs`` loads the real script into Node's built-in
``vm`` module against a stub DOM and asserts the Settings panel closes via
its × control, Escape and an outside click (Node built-ins only).  Skips
cleanly when ``node`` is not on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "board_settings_harness.mjs"


def test_board_settings_js() -> None:
    """Run the Node harness; require a clean (zero) exit code."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH — skipping board-mill.js settings harness")

    assert HARNESS.exists(), f"harness missing: {HARNESS}"

    result = subprocess.run(
        [node, str(HARNESS)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "board-mill.js settings harness failed "
        f"(exit {result.returncode})\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
