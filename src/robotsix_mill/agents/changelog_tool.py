"""Changelog helpers for the implement agent.

The agent-facing tool is ``add_changelog_fragment`` (below), which writes a
towncrier fragment — one file per ticket, so parallel PRs never conflict.

``_insert_changelog_entry`` remains as an internal helper for the
``changelog_autofill`` runner, which repairs CHANGELOG.md on the default
branch rather than on PR branches and so generates no conflicts. It inserts a
bullet under ``## 0.0.0 (unreleased)`` without severing continuation lines
from the existing top entry, handling:
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


# ---------------------------------------------------------------------------
# Fragment writer — the fleet standard's actual mechanism
# ---------------------------------------------------------------------------
#
# ``insert_changelog_entry`` above edits CHANGELOG.md in place, under a single
# ``## 0.0.0 (unreleased)`` header. Every ticket therefore inserts at the SAME
# line, so any two open PRs conflict pairwise — a combinatorial problem that
# grows with how many PRs are in flight, and one ``gh pr update-branch`` cannot
# resolve because both sides changed the same region.
#
# The robotsix stack standard (docs/changelog-driven-releases.md, rule 3) says
# CHANGELOG.md is written only by the release workflow, from per-PR fragments
# under the towncrier directory — precisely so parallel PRs never touch a
# shared file. It allows one exception for a programmatic tool fixing a bug in
# CHANGELOG.md itself, with the explicit caveat that such a tool "must not
# become a general-purpose changelog writer". That is what happened here.
#
# This tool is the standard-conforming replacement: one new file per ticket,
# named for the ticket, so two tickets can never collide.

_DEFAULT_FRAGMENT_DIR = "changelog.d"
_DEFAULT_TYPES = ("feature", "bugfix", "doc", "removal", "misc")


def _towncrier_config(repo_dir: Path) -> tuple[str, tuple[str, ...]]:
    """Return ``(fragment_dir, valid_types)`` from the repo's own pyproject.

    Read per-repo rather than hardcoded because the fleet is not uniform:
    auto-mail keeps fragments in ``changelog/`` while everything else uses
    ``changelog.d/``, and llmio names its types ``feat``/``fix`` where the
    others use ``feature``/``bugfix``. Writing a fragment with the wrong
    extension is silently ignored by ``towncrier build``, so guessing here
    would drop the entry with no error.

    Falls back to the common defaults when pyproject is missing or
    unparseable — a best-effort fragment beats refusing to record anything.
    """
    import tomllib

    pyproject = repo_dir / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except OSError, tomllib.TOMLDecodeError:
        return _DEFAULT_FRAGMENT_DIR, _DEFAULT_TYPES

    section = data.get("tool", {}).get("towncrier", {})
    if not isinstance(section, dict):
        return _DEFAULT_FRAGMENT_DIR, _DEFAULT_TYPES

    directory = section.get("directory")
    fragment_dir = (
        directory if isinstance(directory, str) and directory else _DEFAULT_FRAGMENT_DIR
    )

    types_raw = section.get("type")
    types: list[str] = []
    if isinstance(types_raw, list):
        for entry in types_raw:
            if isinstance(entry, dict):
                name = entry.get("directory")
                if isinstance(name, str) and name:
                    types.append(name)
    return fragment_dir, tuple(types) if types else _DEFAULT_TYPES


def _slugify_ticket(ticket_id: str) -> str:
    """Filesystem-safe stem for a fragment file."""
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in ticket_id)
    return safe.strip("-") or "entry"


def _add_changelog_fragment(
    repo_dir: Path, ticket_id: str, kind: str, summary: str
) -> str:
    """Write ``<fragment_dir>/<ticket_id>.<kind>.md`` containing *summary*."""
    summary = summary.strip()
    if not summary:
        raise ValueError("changelog_fragment: summary must not be empty")

    fragment_dir, valid_types = _towncrier_config(repo_dir)
    if kind not in valid_types:
        raise ValueError(
            f"changelog_fragment: kind {kind!r} is not configured for this repo. "
            f"Valid kinds: {', '.join(valid_types)}. "
            "A fragment with an unconfigured extension is silently skipped by "
            "towncrier build, so the entry would be lost."
        )

    target_dir = repo_dir / fragment_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{_slugify_ticket(ticket_id)}.{kind}.md"
    path.write_text(summary.rstrip() + "\n", encoding="utf-8")
    return f"changelog_fragment: wrote {fragment_dir}/{path.name}"


def make_add_changelog_fragment_tool(
    repo_dir: Path, ticket_id: str
) -> Callable[[str, str], str]:
    """Return the ``add_changelog_fragment`` closure for *repo_dir*/*ticket_id*."""

    def add_changelog_fragment(summary: str, kind: str = "misc") -> str:
        """Record this ticket's changelog entry as a towncrier fragment.

        Writes one file named after the ticket, so parallel tickets never
        touch the same file. Do NOT edit CHANGELOG.md directly — the release
        workflow regenerates it from these fragments, and a hand-written entry
        there is dropped at the next release.

        Args:
            summary: The user-visible description of the change, in prose.
            kind: Fragment type. Must be one configured in the repo's
                ``[tool.towncrier]`` — commonly ``feature``, ``bugfix``,
                ``doc``, ``removal`` or ``misc``.

        Returns:
            A short status string naming the file written.
        """
        return _add_changelog_fragment(repo_dir, ticket_id, kind, summary)

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="add_changelog_fragment",
            description=(
                "Record this ticket's changelog entry as a towncrier "
                "fragment file. One file per ticket, so parallel PRs never "
                "conflict. Never edit CHANGELOG.md directly — the release "
                "workflow regenerates it from these fragments."
            ),
            category="fs",
            parameters={
                "summary": "str (user-visible description)",
                "kind": "str (towncrier type, default 'misc')",
            },
        )
    )

    return add_changelog_fragment
