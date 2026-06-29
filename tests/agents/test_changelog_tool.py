"""Tests for the ``insert_changelog_entry`` agent tool.

The tool replaces the LLM-prompt-driven ``edit_file`` approach to
CHANGELOG.md insertion, which had a ≈42% corruption rate when the
existing top entry spanned multiple continuation lines. The tool
is deterministic — it always preserves the existing top entry's
continuation lines.

The tool contract:

- Non-existent CHANGELOG.md → creates with header + entry.
- Empty section (no bullets) → appends entry after header.
- Single-line top entry → inserts new entry above it.
- Multi-line top entry → inserts before the complete block.
"""

from __future__ import annotations

from pathlib import Path

from robotsix_mill.agents.changelog_tool import _insert_changelog_entry, _HEADER


def test_creates_file_when_missing(tmp_path: Path):
    result = _insert_changelog_entry(tmp_path, "- **foo**: bar")
    assert "created CHANGELOG.md" in result
    content = (tmp_path / "CHANGELOG.md").read_text()
    assert content == f"{_HEADER}\n\n- **foo**: bar\n"


def test_appends_entry_when_section_empty(tmp_path: Path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(f"{_HEADER}\n\n")
    result = _insert_changelog_entry(tmp_path, "- **new**: entry")
    assert "appended entry" in result
    content = changelog.read_text()
    assert content == f"{_HEADER}\n\n- **new**: entry\n"


def test_inserts_before_single_line_top_entry(tmp_path: Path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(f"{_HEADER}\n\n- **old**: first\n")
    result = _insert_changelog_entry(tmp_path, "- **new**: entry")
    assert "inserted entry before existing top entry" in result
    content = changelog.read_text()
    assert content == f"{_HEADER}\n\n- **new**: entry\n- **old**: first\n"


def test_inserts_before_multi_line_top_entry(tmp_path: Path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        f"{_HEADER}\n\n"
        "- **old**: first line\n"
        "  continuation line 1\n"
        "  continuation line 2\n"
        "- **second**: bullet\n"
    )
    result = _insert_changelog_entry(tmp_path, "- **new**: entry")
    assert "inserted entry before existing top entry" in result
    content = changelog.read_text()
    expected = (
        f"{_HEADER}\n\n"
        "- **new**: entry\n"
        "- **old**: first line\n"
        "  continuation line 1\n"
        "  continuation line 2\n"
        "- **second**: bullet\n"
    )
    assert content == expected


def test_preserves_continuation_with_tab_indent(tmp_path: Path):
    """Tab-indented continuations (rare but valid markdown) are preserved."""
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        f"{_HEADER}\n\n"
        "- **old**: first line\n"
        "\tcontinued with tab\n"
        "- **second**: bullet\n"
    )
    result = _insert_changelog_entry(tmp_path, "- **new**: entry")
    assert "inserted entry before existing top entry" in result
    content = changelog.read_text()
    expected = (
        f"{_HEADER}\n\n"
        "- **new**: entry\n"
        "- **old**: first line\n"
        "\tcontinued with tab\n"
        "- **second**: bullet\n"
    )
    assert content == expected


def test_handles_entry_with_continuation_lines(tmp_path: Path):
    """The new entry itself can have continuation lines."""
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(f"{_HEADER}\n\n- **old**: single\n")
    new_entry = "- **new**: multi-line\n  detail line 1\n  detail line 2"
    result = _insert_changelog_entry(tmp_path, new_entry)
    assert "inserted entry before existing top entry" in result
    content = changelog.read_text()
    expected = (
        f"{_HEADER}\n\n"
        "- **new**: multi-line\n"
        "  detail line 1\n"
        "  detail line 2\n"
        "- **old**: single\n"
    )
    assert content == expected


def test_rejects_entry_without_bullet_prefix(tmp_path: Path):
    result = _insert_changelog_entry(tmp_path, "plain text without bullet")
    assert "must start with '- '" in result


def test_adds_header_when_missing(tmp_path: Path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Old header\n\n- old entry\n")
    result = _insert_changelog_entry(tmp_path, "- **new**: entry")
    assert "added header + entry" in result
    content = changelog.read_text()
    assert content.startswith(f"{_HEADER}\n\n- **new**: entry\n")
    assert "old entry" in content
