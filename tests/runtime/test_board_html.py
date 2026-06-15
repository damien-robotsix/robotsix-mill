"""Unit tests for robotsix_mill.runtime.board_html.

Exercises ``asset_version``, ``build_board_skeleton``, and
``render_board_html`` covering normal operation, edge cases
(empty columns, special characters, missing placeholders), and
the env-var / fallback path for the cache-busting token.
"""

from __future__ import annotations

import html as _html
import re

from robotsix_mill.runtime.board_html import (
    BOARD_HTML,
    _PROCESS_START_TOKEN,
    asset_version,
    build_board_skeleton,
    render_board_html,
)


# ---------------------------------------------------------------------------
# asset_version
# ---------------------------------------------------------------------------


def test_asset_version_uses_env_var(monkeypatch) -> None:
    """When MILL_BUILD_SHA is set and non-empty, it is returned."""
    monkeypatch.setenv("MILL_BUILD_SHA", "abc1234")
    # Clear the lru_cache so the env var is re-read.
    asset_version.cache_clear()
    try:
        assert asset_version() == "abc1234"
    finally:
        asset_version.cache_clear()


def test_asset_version_ignores_empty_env(monkeypatch) -> None:
    """When MILL_BUILD_SHA is empty or whitespace, the process-start
    token is used instead."""
    monkeypatch.setenv("MILL_BUILD_SHA", "   ")
    asset_version.cache_clear()
    try:
        assert asset_version() == _PROCESS_START_TOKEN
    finally:
        asset_version.cache_clear()


def test_asset_version_falls_back_to_process_token(monkeypatch) -> None:
    """When MILL_BUILD_SHA is absent, the process-start token is used."""
    monkeypatch.delenv("MILL_BUILD_SHA", raising=False)
    asset_version.cache_clear()
    try:
        assert asset_version() == _PROCESS_START_TOKEN
    finally:
        asset_version.cache_clear()


# ---------------------------------------------------------------------------
# build_board_skeleton
# ---------------------------------------------------------------------------


def test_build_board_skeleton_empty() -> None:
    """An empty column list produces the #board wrapper with no columns."""
    html = build_board_skeleton([])
    assert html == '<div id="board" class="board"></div>'


def test_build_board_skeleton_single_column() -> None:
    """A single column renders one .board-column with header and cards slot."""
    html = build_board_skeleton([("draft", "Draft")])
    assert 'id="board"' in html
    assert 'class="board"' in html
    assert 'data-status="draft"' in html
    assert ">Draft<" in html
    assert 'class="board-column-cards"' in html
    assert 'class="board-column-count"' in html
    assert html.count("board-column") == 5  # column + header + label + count + cards
    assert html.startswith('<div id="board" class="board">')
    assert html.endswith("</div>")


def test_build_board_skeleton_multiple_columns() -> None:
    """Multiple columns render in the given order."""
    columns = [
        ("epic_open", "Epic Open"),
        ("draft", "Draft"),
        ("ready", "Ready"),
        ("done", "Done"),
    ]
    html = build_board_skeleton(columns)
    # Each column has a data-status attribute.
    statuses = re.findall(r'data-status="([^"]*)"', html)
    assert statuses == ["epic_open", "draft", "ready", "done"]


def test_build_board_skeleton_html_escapes_labels() -> None:
    """Labels containing HTML special chars are escaped."""
    html = build_board_skeleton([("x", '<script>alert("xss")</script>')])
    assert "<script>" not in html
    assert _html.escape('<script>alert("xss")</script>', quote=True) in html


def test_build_board_skeleton_html_escapes_keys() -> None:
    """Status keys containing quotes are escaped."""
    html = build_board_skeleton([('has"quote', "Label")])
    assert 'data-status="has&quot;quote"' in html


# ---------------------------------------------------------------------------
# render_board_html
# ---------------------------------------------------------------------------


def test_render_board_html_substitutes_all_placeholders() -> None:
    """All three placeholders ({ASSET_VERSION}, {CONFIG_SCRIPT},
    {BOARD_SKELETON}) are replaced."""
    result = render_board_html(
        config_script="<script>CONFIG</script>",
        skeleton='<div id="board">skel</div>',
    )
    assert "{ASSET_VERSION}" not in result
    assert "{CONFIG_SCRIPT}" not in result
    assert "{BOARD_SKELETON}" not in result


def test_render_board_html_includes_config_script() -> None:
    """The config_script string is embedded in the output."""
    result = render_board_html(
        config_script="<script>window.CONFIG = {};</script>",
        skeleton="",
    )
    assert "<script>window.CONFIG = {};</script>" in result


def test_render_board_html_includes_skeleton() -> None:
    """The skeleton string is embedded in the output."""
    result = render_board_html(
        config_script="",
        skeleton='<div id="board">HELLO</div>',
    )
    assert '<div id="board">HELLO</div>' in result


def test_render_board_html_asset_version_in_urls() -> None:
    """All local static asset URLs carry the cache-busting version token."""
    result = render_board_html(config_script="", skeleton="")
    version = asset_version()
    assert f"/static/board.css?v={version}" in result
    assert f"/static/mill/board-mill.css?v={version}" in result
    assert f"/static/board.js?v={version}" in result
    assert f"/static/mill/board-mill.js?v={version}" in result


def test_render_board_html_empty_args_still_valid_html() -> None:
    """Passing empty strings for config_script and skeleton produces
    valid-looking HTML (doctype, html, head, body tags)."""
    result = render_board_html(config_script="", skeleton="")
    assert result.startswith("<!doctype html>")
    assert "<html>" in result
    assert "<head>" in result
    assert "<body>" in result
    assert "</html>" in result
    # The BOARD_SKELETON placeholder should be replaced with nothing.
    assert "{BOARD_SKELETON}" not in result


def test_render_board_html_preserves_cdn_script() -> None:
    """The external marked.js CDN URL is present and is NOT versioned."""
    result = render_board_html(config_script="", skeleton="")
    assert "cdn.jsdelivr.net/npm/marked" in result
    # The CDN URL must never carry the local asset version.
    version = asset_version()
    assert f"marked@15.0.12/lib/marked.umd.js?v={version}" not in result


def test_render_board_html_skeleton_appears_after_header() -> None:
    """The skeleton is injected after the closing </header> tag."""
    result = render_board_html(
        config_script="",
        skeleton='<div id="board">SKEL</div>',
    )
    header_close = result.index("</header>")
    skeleton_pos = result.index("SKEL")
    assert skeleton_pos > header_close


# ---------------------------------------------------------------------------
# BOARD_HTML constant
# ---------------------------------------------------------------------------


def test_board_html_constant_has_placeholders() -> None:
    """The BOARD_HTML template string contains the three expected
    placeholders so render_board_html can substitute them."""
    assert "{ASSET_VERSION}" in BOARD_HTML
    assert "{CONFIG_SCRIPT}" in BOARD_HTML
    assert "{BOARD_SKELETON}" in BOARD_HTML
