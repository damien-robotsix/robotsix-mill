"""A ``insert_changelog_entry`` tool that inserts a new bullet entry
under ``## 0.0.0 (unreleased)`` in CHANGELOG.md without severing
continuation lines from the existing top entry.

The tool handles:
- Non-existent CHANGELOG.md → creates it with header + entry.
- Empty section (no bullets yet) → appends entry after header.
- Single-line top entry → inserts new entry above it.
- Multi-line top entry (continuation lines indented with 2 spaces) →
  inserts BEFORE the complete block so continuation lines stay attached.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

_HEADER = "## 0.0.0 (unreleased)"


def _find_header_idx(lines: list[str]) -> int | None:
    """Return the index of the ``## 0.0.0 (unreleased)`` header line, or None."""
    for i, line in enumerate(lines):
        if line.strip() == _HEADER:
            return i
    return None


def _find_first_bullet(lines: list[str], start: int) -> int | None:
    """Return the index of the first ``- `` bullet line at or after *start*."""
    for i in range(start, len(lines)):
        if lines[i].strip().startswith("- "):
            return i
    return None


def _insert_changelog_entry(repo_dir: Path, entry_text: str) -> str:
    """Imperative insertion logic (testable without LLM wiring)."""
    changelog_path = repo_dir / "CHANGELOG.md"
    entry_text = entry_text.strip()
    if not entry_text.startswith("- "):
        return (
            "changelog_insert: entry_text must start with '- ' "
            f"(got {entry_text[:40]!r})"
        )

    if not changelog_path.exists():
        changelog_path.write_text(f"{_HEADER}\n\n{entry_text}\n", encoding="utf-8")
        return "changelog_insert: created CHANGELOG.md with header + entry"

    lines = changelog_path.read_text(encoding="utf-8").splitlines(keepends=True)

    header_idx = _find_header_idx(lines)
    if header_idx is None:
        lines.insert(0, f"{_HEADER}\n\n{entry_text}\n")
        changelog_path.write_text("".join(lines), encoding="utf-8")
        return "changelog_insert: added header + entry at top"

    first_bullet_idx = _find_first_bullet(lines, header_idx + 1)
    if first_bullet_idx is None:
        # No bullets in the section yet — insert after header + blank line.
        insert_at = header_idx + 1
        while insert_at < len(lines) and lines[insert_at].strip() == "":
            insert_at += 1
        if insert_at > header_idx + 1:
            lines.insert(insert_at, f"{entry_text}\n")
        else:
            lines.insert(insert_at, f"\n{entry_text}\n")
        changelog_path.write_text("".join(lines), encoding="utf-8")
        return "changelog_insert: appended entry (section was empty)"

    # Find the end of the first entry block and insert before it.
    lines.insert(first_bullet_idx, f"{entry_text}\n")
    changelog_path.write_text("".join(lines), encoding="utf-8")
    return "changelog_insert: inserted entry before existing top entry"


def make_insert_changelog_entry_tool(repo_dir: Path) -> Callable[[str], str]:
    """Return the ``insert_changelog_entry`` closure bound to *repo_dir*.

    The returned function is synchronous (file I/O only) so it works
    with both sync and async pydantic-ai tool dispatch.
    """

    def insert_changelog_entry(entry_text: str) -> str:
        """Insert a new bullet entry at the top of the ``## 0.0.0
        (unreleased)`` section in CHANGELOG.md.

        Handles multi-line continuation correctly — continuation lines
        (indented with 2 spaces) stay attached to their parent bullet.

        Args:
            entry_text: The full entry text including the leading ``-
                `` bullet.  Can span multiple lines (continuation lines
                indented with 2 spaces).

        Returns:
            A short status string.
        """
        return _insert_changelog_entry(repo_dir, entry_text)

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="insert_changelog_entry",
            description=(
                "Insert a new bullet entry at the top of the "
                "``## 0.0.0 (unreleased)`` section in CHANGELOG.md. "
                "Correctly handles multi-line continuation — "
                "continuation lines stay attached to their parent bullet."
            ),
            category="fs",
            parameters={"entry_text": "str (bullet + optional continuation lines)"},
        )
    )

    return insert_changelog_entry
