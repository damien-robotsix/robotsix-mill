"""Drive the board-mill.js "Remove repo from mill" harness from pytest.

``board_remove_repo_harness.mjs`` loads the real board-mill.js into
Node's built-in ``vm`` module and asserts the confirmation modal, the
``DELETE /repos/{id}`` call, cancel/error handling and the post-removal
redirect + toast.  Skips cleanly when ``node`` is not on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "board_remove_repo_harness.mjs"


def test_board_remove_repo_js() -> None:
    """Run the Node harness; require a clean (zero) exit code."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH — skipping board-mill.js remove-repo harness")

    assert HARNESS.exists(), f"harness missing: {HARNESS}"

    result = subprocess.run([node, str(HARNESS)], capture_output=True, text=True)
    assert result.returncode == 0, (
        "board-mill.js remove-repo harness failed "
        f"(exit {result.returncode})\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
