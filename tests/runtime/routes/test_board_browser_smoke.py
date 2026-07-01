"""Tests for the Tier-2 browser-level board smoke check.

The live happy-path test drives the real ``scripts/board_browser_check.py``
flow (seed → serve → headless Chromium) and therefore skips cleanly when
Playwright or a launchable Chromium are absent — mirroring how the
``tests/runtime/test_board_*_js.py`` harnesses skip when ``node`` is not on
PATH. This keeps normal CI green while the full browser check runs only in
the Chromium-equipped sandbox image / smoke gate.

The remaining tests exercise the driver's browser-free helpers
(``classify_console_error`` / ``check_column_count``) directly — no live
browser required — proving the failure paths that make the gate
non-flaky: same-origin errors are fatal, blocked-CDN errors are ignored,
and an empty kanban fails.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "board_browser_check.py"


def _load_driver():
    """Import ``scripts/board_browser_check.py`` as a module by path.

    The script lives outside the importable package tree, so load it via
    an explicit file-location spec. This import is browser-free (the
    Playwright import is deferred inside the driver's functions).
    """
    spec = importlib.util.spec_from_file_location("board_browser_check", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bbc = _load_driver()

_ORIGIN = "http://127.0.0.1:8077"


# --------------------------------------------------------------------------
# Browser-free helper unit tests.
# --------------------------------------------------------------------------
def test_same_origin_console_error_is_fatal() -> None:
    assert bbc.classify_console_error(_ORIGIN + "/static/board.js", _ORIGIN) is True
    assert (
        bbc.classify_console_error(_ORIGIN + "/static/mill/board-mill.js", _ORIGIN)
        is True
    )


def test_inline_console_error_is_fatal() -> None:
    # Inline / eval errors carry no source URL — they originate from the
    # page itself and must be treated as same-origin (fatal).
    assert bbc.classify_console_error("", _ORIGIN) is True


def test_jsdelivr_console_error_is_non_fatal() -> None:
    blocked = "https://cdn.jsdelivr.net/npm/marked@15.0.12/lib/marked.umd.js"
    assert bbc.classify_console_error(blocked, _ORIGIN) is False


def test_check_column_count_zero_fails() -> None:
    with pytest.raises(AssertionError):
        bbc.check_column_count(0, 22)


def test_check_column_count_mismatch_fails() -> None:
    with pytest.raises(AssertionError):
        bbc.check_column_count(5, 22)


def test_check_column_count_matches_adapter() -> None:
    from robotsix_mill.runtime.board_adapter import _COLUMNS

    # No raise when the painted count equals the adapter's column count.
    bbc.check_column_count(len(_COLUMNS), len(_COLUMNS))


def test_screenshot_target_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOARD_SMOKE_SCREENSHOT", raising=False)
    assert bbc._screenshot_target() is None


def test_screenshot_target_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOARD_SMOKE_SCREENSHOT", "")
    assert bbc._screenshot_target() is None
    monkeypatch.setenv("BOARD_SMOKE_SCREENSHOT", "   ")
    assert bbc._screenshot_target() is None


def test_screenshot_target_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOARD_SMOKE_SCREENSHOT", "  artifacts/board.png  ")
    assert bbc._screenshot_target() == "artifacts/board.png"


# --------------------------------------------------------------------------
# Live happy-path test — skips unless Playwright + Chromium are available.
# --------------------------------------------------------------------------
def _chromium_launchable() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:
        return False


def test_board_browser_smoke_happy_path() -> None:
    pytest.importorskip("playwright")
    if not _chromium_launchable():
        pytest.skip("Chromium not installed/launchable — skipping browser smoke")
    assert bbc.run_browser_check() == 0
