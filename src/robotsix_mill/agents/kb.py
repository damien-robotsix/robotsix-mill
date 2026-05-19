"""Knowledge base loader.

A KB entry is ``kb/*.md`` or ``kb/*/KB.md`` — a plain Markdown file
describing a technology-specific constraint, gotcha, or workaround.
Unlike skills (which teach *tool usage*), KB entries are domain
knowledge about the project's stack — e.g. "SQLite strips tzinfo on
DateTime". Every entry is always included (no frontmatter needed).

The loader concatenates them into a prompt section for the refine agent
so it can avoid writing specs that prescribe impossible things.
"""

from __future__ import annotations

from pathlib import Path


def load_kb(kb_dir: Path) -> str:
    """Return a prompt section describing every known technology
    constraint, or ``""`` if the directory is missing or empty.

    Reads ``*.md`` files directly in ``kb_dir`` AND ``*/KB.md`` files
    one level deep (mirrors ``load_skills`` glob pattern but without
    YAML frontmatter parsing).
    """
    d = Path(kb_dir)
    if not d.is_dir():
        return ""

    blocks: list[str] = []
    # Flat .md files at the top level, plus */KB.md one level down.
    paths = sorted(d.glob("*.md")) + sorted(d.glob("*/KB.md"))
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            blocks.append(text)

    if not blocks:
        return ""

    return (
        "\n\n# Technology Constraints\n\n"
        "The project's technology stack has the following known "
        "constraints and gotchas. When writing specs, do NOT prescribe "
        "anything that violates these constraints.\n\n"
        + "\n\n---\n\n".join(blocks)
    )
