"""Drive the board-mill.js refresh-filter / status-bar JS tests from pytest.

The repo-filter bootstrap and the ``#meta`` status-bar updater live in
``src/robotsix_mill/runtime/static/board-mill.js`` — a flat browser IIFE
with no Python seam and no JS test runner in the repo.
``board_refresh_filter_harness.mjs`` loads the real script into Node's
built-in ``vm`` module against a stub DOM/window and asserts that the
bootstrap sets the filtered refresh URL even when ``document.readyState``
is already ``"complete"`` (the cached/bfcache F5 path) and that
``updateMeta()`` replaces the permanent ``"loading…"`` placeholder, using
only Node built-ins (no ``npm install``).

This wrapper shells out to ``node`` so the harness runs inside the
existing ``uv run pytest`` CI step with no Node-specific CI changes. It
skips cleanly when ``node`` is not on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "board_refresh_filter_harness.mjs"


def test_board_refresh_filter_js() -> None:
    """Run the Node harness; require a clean (zero) exit code."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH — skipping board-mill.js refresh-filter harness")

    assert HARNESS.exists(), f"harness missing: {HARNESS}"

    result = subprocess.run(
        [node, str(HARNESS)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "board-mill.js refresh-filter harness failed "
        f"(exit {result.returncode})\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
