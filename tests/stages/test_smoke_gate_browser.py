"""Tier 2 §4 wiring: the board browser smoke is registered into Tier 1's
path-scoped smoke gate.

These tests are browser-free — they exercise the *selection* logic (does
the gate fire for this diff?) and the *registration* recorded in the
repo's own ``.robotsix-mill/config.yaml``, never Playwright/Chromium.
"""

from pathlib import Path

from robotsix_mill.agents.testing import smoke_paths_match
from robotsix_mill.config.repo_settings import (
    load_repo_smoke_command,
    load_repo_smoke_paths,
)

# Repo root: tests/stages/<file> → parents[2].
REPO_ROOT = Path(__file__).resolve().parents[2]


def test_repo_config_registers_browser_smoke_with_screenshot():
    """The mill repo's ``.robotsix-mill/config.yaml`` wires the Tier 2
    browser wrapper into the smoke command and hands it the screenshot
    path the review stage reads (``artifacts/board.png``)."""
    cmd = load_repo_smoke_command(REPO_ROOT)
    assert cmd is not None
    assert "scripts/smoke_board_browser.sh" in cmd
    # The wrapper is invoked with BOARD_SMOKE_SCREENSHOT pointing at the
    # path the review stage consumes (stages/review.py reads board.png).
    assert "BOARD_SMOKE_SCREENSHOT=artifacts/board.png" in cmd
    # Tier 1's API-level smoke is preserved alongside the browser smoke.
    assert "scripts/smoke_board.sh" in cmd


def test_board_ui_diff_selects_the_browser_smoke():
    """A diff touching the board/runtime UI surface matches the configured
    smoke_paths, so the gate (and its browser smoke) fires."""
    smoke_paths = load_repo_smoke_paths(REPO_ROOT)
    assert smoke_paths, "repo must declare board-UI smoke_paths"
    changed = ["src/robotsix_mill/runtime/static/board.js"]
    assert smoke_paths_match(changed, smoke_paths) is True


def test_non_board_diff_skips_the_browser_smoke():
    """A diff touching only non-board-UI paths matches no smoke glob, so
    the gate is skipped and no browser check runs (review stays
    text-only)."""
    smoke_paths = load_repo_smoke_paths(REPO_ROOT)
    changed = ["docs/modules.yaml", "src/robotsix_mill/config.py"]
    assert smoke_paths_match(changed, smoke_paths) is False
