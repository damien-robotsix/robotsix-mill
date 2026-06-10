"""Browser-level smoke check for the mill kanban board (Tier 2).

Seeds a temporary DB with a handful of distinctively-titled tickets,
serves the board on loopback against that DB, loads ``GET /`` in headless
Chromium via Playwright, and asserts that:

* the kanban skeleton paints (``#board`` present and the number of
  ``.board-column`` elements equals the adapter's runtime column count),
* every seeded ticket title renders in the DOM after client-side
  hydration, and
* no *same-origin* console ``error`` or uncaught ``pageerror`` occurs.

Console errors sourced from blocked third-party CDNs (e.g. the jsdelivr
``marked`` fetch, which the sandbox egress proxy refuses) are classified
as **non-fatal** so the gate's verdict does not depend on external
network availability.

The assertion / classification helpers (:func:`is_same_origin`,
:func:`classify_console_error`, :func:`check_column_count`,
:func:`_screenshot_target`) are import-safe and browser-free so they can
be unit-tested without Playwright or Chromium installed. The Playwright
import is deferred into :func:`_drive_browser` for the same reason.

When the ``BOARD_SMOKE_SCREENSHOT`` environment variable is set to a
non-empty path, the driver captures a full-page PNG of the rendered board
to that path *after* all DOM/console assertions pass. When the variable
is unset or empty, capture is skipped and the run stays side-effect-free.

Exit code: ``0`` when the board renders correctly, non-zero otherwise.
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# Distinctive, assert-able seed titles paired with the board column the
# ticket should land in. ``draft`` is the create() default; ``ready`` and
# ``done`` are reachable directly from DRAFT via TicketService.transition.
SEED_TICKETS: list[tuple[str, str]] = [
    ("SMOKE-TICKET-ALPHA", "draft"),
    ("SMOKE-TICKET-BRAVO", "ready"),
    ("SMOKE-TICKET-CHARLIE", "done"),
    ("SMOKE-TICKET-DELTA", "draft"),
]


# --------------------------------------------------------------------------
# Browser-free assertion / classification helpers (unit-testable).
# --------------------------------------------------------------------------
def is_same_origin(source_url: str, app_origin: str) -> bool:
    """Return whether *source_url* belongs to the app's own origin.

    An empty/missing URL (inline-script or eval errors carry no source
    URL) is treated as same-origin — those originate from the page
    itself, not a third party.
    """
    if not source_url:
        return True
    return source_url.startswith(app_origin)


def classify_console_error(source_url: str, app_origin: str) -> bool:
    """Classify a console ``error`` as fatal (``True``) or not (``False``).

    A console error is **fatal** when its source location is same-origin
    (the app's own ``/static/board.js``, ``/static/mill/board-mill.js``,
    or an inline script). Errors sourced from blocked external CDNs
    (e.g. ``cdn.jsdelivr.net`` serving ``marked``) are **non-fatal** —
    they only reflect external network availability, not a board bug.
    """
    return is_same_origin(source_url, app_origin)


def check_column_count(actual: int, expected: int) -> None:
    """Assert the painted column count matches the adapter's expectation.

    Raises ``AssertionError`` when the kanban is missing/empty (zero
    columns) or when the count diverges from *expected*.
    """
    if expected <= 0:
        raise AssertionError(f"expected column count must be positive, got {expected}")
    if actual <= 0:
        raise AssertionError("kanban not painted: zero .board-column elements rendered")
    if actual != expected:
        raise AssertionError(
            f".board-column count {actual} != expected {expected} "
            "(board_adapter._COLUMNS)"
        )


def _screenshot_target() -> str | None:
    """Return the screenshot output path from ``BOARD_SMOKE_SCREENSHOT``.

    Returns the stripped path when the env var is set to a non-empty
    value, or ``None`` when it is unset or empty/whitespace. Browser-free
    and import-safe so it is unit-testable without Chromium.
    """
    value = os.environ.get("BOARD_SMOKE_SCREENSHOT", "").strip()
    return value or None


def _capture_screenshot(page: object, screenshot_path: str | None) -> None:
    """Write a full-page PNG of *page* to *screenshot_path*.

    A falsy *screenshot_path* is a no-op. Otherwise creates parent
    directories as needed. Called only once the board is confirmed
    healthy so a broken board never yields a misleading image.
    """
    if not screenshot_path:
        return
    Path(screenshot_path).parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=screenshot_path, full_page=True)  # type: ignore[attr-defined]
    print(f"  (screenshot written to {screenshot_path})")


# --------------------------------------------------------------------------
# Server + DB plumbing.
# --------------------------------------------------------------------------
def _free_loopback_socket() -> socket.socket:
    """Bind and return a loopback socket on an ephemeral port.

    Handing the bound socket directly to uvicorn avoids a bind/connect
    race on the chosen port. ``127.0.0.1`` is ``NO_PROXY``-exempt so the
    browser reaches it without going through the egress proxy.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    return sock


def _seed_db(settings: object, board_id: str) -> None:
    """Seed *board_id*'s DB with the SEED_TICKETS across several columns."""
    from robotsix_mill.core import db
    from robotsix_mill.core.service import TicketService
    from robotsix_mill.core.states import State

    db.reset_engine()
    db.init_db(settings, board_id=board_id)
    svc = TicketService(settings, board_id=board_id)
    targets = {"ready": State.READY, "done": State.DONE}
    for title, column in SEED_TICKETS:
        ticket = svc.create(title=title, description=f"Smoke seed for {title}")
        target = targets.get(column)
        if target is not None:
            svc.transition(ticket.id, target, "smoke seed")


def _wait_for_health(base_url: str, timeout: float = 30.0) -> None:
    """Poll ``GET /health`` until it returns 200 or *timeout* elapses."""
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(  # noqa: S310 — fixed loopback URL
                base_url + "/health", timeout=2
            ) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, OSError) as exc:
            last_err = exc
        time.sleep(0.1)
    raise RuntimeError(f"server did not become healthy within {timeout}s: {last_err}")


def _drive_browser(
    base_url: str,
    expected_columns: int,
    screenshot_path: str | None = None,
) -> None:
    """Load ``GET /`` in headless Chromium and run the board assertions.

    When *screenshot_path* is truthy, a full-page PNG of the rendered
    board is written there *after* all assertions pass (never on a
    failing board), creating parent directories as needed.
    """
    from playwright.sync_api import sync_playwright

    console_error_urls: list[str] = []
    page_errors: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()

            def _on_console(msg: object) -> None:
                if getattr(msg, "type", None) == "error":
                    location = getattr(msg, "location", None) or {}
                    console_error_urls.append(location.get("url", ""))

            def _on_pageerror(exc: object) -> None:
                page_errors.append(str(exc))

            page.on("console", _on_console)
            page.on("pageerror", _on_pageerror)

            page.goto(base_url + "/", wait_until="domcontentloaded")
            # Cards hydrate client-side via board.js fetching /board/cards;
            # wait for a card element before asserting on the DOM. Match both
            # robotsix-board's (.board-card) and board-mill.js's (.card) shapes.
            page.wait_for_selector(".board-card, .card", timeout=15_000)

            # Kanban structure painted.
            if page.query_selector("#board") is None:
                raise AssertionError("#board container missing from the DOM")
            column_count = len(page.query_selector_all(".board-column"))
            check_column_count(column_count, expected_columns)

            # Seeded tickets visible (match on title text — robust across the
            # two card shapes).
            body_text = page.inner_text("body")
            for title, _ in SEED_TICKETS:
                if title not in body_text:
                    raise AssertionError(
                        f"seeded ticket title not rendered in the board: {title}"
                    )

            # Console / JS errors — fail on any pageerror or same-origin
            # console error; log (but tolerate) blocked external-CDN errors.
            if page_errors:
                raise AssertionError(f"uncaught pageerror(s): {page_errors}")
            fatal = [
                u for u in console_error_urls if classify_console_error(u, base_url)
            ]
            non_fatal = [
                u for u in console_error_urls if not classify_console_error(u, base_url)
            ]
            for url in non_fatal:
                print(f"  (ignored non-fatal external console error: {url})")
            if fatal:
                raise AssertionError(
                    "same-origin console error(s) detected: "
                    + ", ".join(u or "<inline>" for u in fatal)
                )

            # Board confirmed healthy — capture visual evidence only now so
            # a broken board never yields a misleading 'healthy' image.
            _capture_screenshot(page, screenshot_path)
        finally:
            browser.close()


def run_browser_check() -> int:
    """Run the full seed → serve → browser smoke flow.

    Returns ``0`` when the board renders correctly, ``1`` otherwise.
    Browser, server, and temp data dir are always torn down.
    """
    import uvicorn

    from robotsix_mill.config import RepoConfig, ReposRegistry, Settings
    from robotsix_mill.core import db
    from robotsix_mill.runtime.api import create_app
    from robotsix_mill.runtime.board_adapter import _COLUMNS

    board_id = "smoke-board"
    repo_id = "smoke-repo"
    tmpdir = tempfile.mkdtemp(prefix="board-browser-smoke-")
    server: uvicorn.Server | None = None
    thread: threading.Thread | None = None
    try:
        settings = Settings(data_dir=tmpdir, require_approval="false")
        _seed_db(settings, board_id)

        repos = ReposRegistry(
            repos={
                repo_id: RepoConfig(
                    repo_id=repo_id,
                    board_id=board_id,
                    langfuse_project_name="smoke",
                    langfuse_public_key="pk-smoke",
                    langfuse_secret_key="sk-smoke",
                )
            }
        )
        app = create_app(repos, settings, single_repo_id=repo_id)

        sock = _free_loopback_socket()
        port = sock.getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"

        config = uvicorn.Config(app, log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": [sock]}, daemon=True
        )
        thread.start()

        _wait_for_health(base_url)
        _drive_browser(base_url, len(_COLUMNS), screenshot_path=_screenshot_target())
        print("board browser smoke check: PASS")
        return 0
    except Exception as exc:  # noqa: BLE001 — top-level smoke driver
        print(f"board browser smoke check: FAIL — {exc}", file=sys.stderr)
        return 1
    finally:
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=10)
        db.reset_engine()
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(run_browser_check())
