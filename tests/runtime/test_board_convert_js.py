"""Drive the board-mill.js Convert-to-Ticket JS tests from pytest.

The Convert-to-Ticket button + flow lives in
``src/robotsix_mill/runtime/static/board-mill.js`` — a browser script
with no Python seam and no JS test runner in the repo.
``board_convert_harness.mjs`` loads the real script into Node's
built-in ``vm`` module against a stub DOM/XHR and asserts the button
gating and conversion-flow behaviour using only Node built-ins (no
``npm install``).

This wrapper shells out to ``node`` so the harness runs inside the
existing ``uv run pytest`` CI step with no Node-specific CI changes.
It skips cleanly when ``node`` is not on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "board_convert_harness.mjs"


def test_board_convert_to_ticket_js() -> None:
    """Run the Node harness; require a clean (zero) exit code."""
    node = shutil.which("node")
    if node is None:
        pytest.skip(
            "node not on PATH — skipping board-mill.js convert-to-ticket harness"
        )

    assert HARNESS.exists(), f"harness missing: {HARNESS}"

    result = subprocess.run(
        [node, str(HARNESS)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "board-mill.js convert-to-ticket harness failed "
        f"(exit {result.returncode})\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
