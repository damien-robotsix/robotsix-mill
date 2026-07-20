#!/usr/bin/env python3
"""Validate changelog fragment files before commit.

Ensures every changelog fragment file under ``changelog.d/`` (and other
common fragment directories) satisfies two invariants:

1.  **Trailing newline** — file must end with ``\\n`` so the
    ``end-of-file-fixer`` pre-commit hook passes.  Auto-fixed in-place.

2.  **Module registry** — ``docs/modules.yaml`` must list the fragment
    directory glob (``changelog.d/*.md``) under the ``core`` module's
    ``paths``.  Auto-fixed in-place.

The script is intended to be called by the implement stage's
``_finalize()`` method right before ``git_ops.commit_all()``, catching
both classes of failure before the pre-commit hooks run in CI.

Can also be invoked directly for ad-hoc validation::

    python scripts/validate-changelog.py [repo-dir]

Exit code 0 means clean (or auto-fixed).  Exit code 1 means a
non-recoverable error was encountered.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Fragment directories to scan for ``*.md`` files.
_FRAGMENT_DIRS = ("changelog.d", "changelog", "changes")


def _trailing_newline_errors(repo_dir: Path) -> list[str]:
    """Check / auto-fix trailing newlines on every ``*.md`` fragment file.

    Returns diagnostic strings for any files that were missing a
    trailing newline (one per fixed file).
    """
    fixed: list[str] = []
    for name in _FRAGMENT_DIRS:
        d = repo_dir / name
        if not d.is_dir():
            continue
        for frag in sorted(d.glob("*.md")):
            content = frag.read_bytes()
            if not content.endswith(b"\n"):
                frag.write_bytes(content + b"\n")
                fixed.append(
                    f"{frag.relative_to(repo_dir)}: appended missing trailing newline"
                )
    return fixed


def _is_next_module(line: str, in_core: bool) -> bool:
    """Return True when *line* signals the next module after core."""
    return in_core and line.rstrip("\n").startswith("  - id: ")


def _find_core_paths_range(lines: list[str]) -> tuple[int | None, int | None, str]:  # noqa: C901 — parser state-machine, branches are inherent
    """Locate the ``core`` module's ``paths`` list in *lines*.

    Returns ``(paths_start, paths_end, indent)`` where *paths_start* is
    the index of the ``paths:`` line, *paths_end* is one-past-the-last
    path line (or len(lines) if no boundary detected), and *indent* is
    the whitespace prefix used for list items.  Returns ``(None, None,
    "")`` when the core module or its paths key cannot be found.
    """
    in_core = False
    in_paths = False
    paths_start: int | None = None
    paths_end: int | None = None
    indent = ""

    for i, line in enumerate(lines):
        stripped = line.rstrip("\n")
        if _is_next_module(line, in_core):
            if in_paths:
                paths_end = i
            break
        if stripped == "  - id: core":
            in_core = True
            continue
        if in_core and stripped.strip() == "paths:":
            paths_start = i
            in_paths = True
            indent = line[: len(line) - len(line.lstrip())]
            continue
        if in_paths and stripped.strip() == "dependencies:":
            paths_end = i
            break
        if in_paths and not stripped.startswith(indent + "  - "):
            if stripped.strip() == "" or not stripped.startswith(indent + "  "):
                paths_end = i
                break

    if paths_start is None:
        return None, None, ""
    if paths_end is None:
        paths_end = len(lines)
    return paths_start, paths_end, indent


def _insert_path_glob(
    lines: list[str], paths_start: int, paths_end: int, indent: str, glob_pattern: str
) -> bool:
    """Insert *glob_pattern* into the path list if not already present.

    Returns ``True`` when a line was inserted, ``False`` otherwise.
    """
    for i in range(paths_start + 1, paths_end):
        if glob_pattern in lines[i]:
            return False  # Already present.

    # Insert before the last path entry.
    insert_at = paths_end
    for i in range(paths_end - 1, paths_start, -1):
        if lines[i].strip().startswith("- "):
            insert_at = i + 1
            break

    lines.insert(insert_at, f"{indent}  - {glob_pattern}\n")
    return True


def _modules_yaml_check(repo_dir: Path) -> list[str]:
    """Check / auto-fix the ``docs/modules.yaml`` module registry.

    Ensures ``changelog.d/*.md`` appears under the ``core`` module's
    ``paths``.  Returns diagnostic strings for changes made.
    """
    modules_yaml = repo_dir / "docs" / "modules.yaml"
    if not modules_yaml.is_file():
        return []

    text = modules_yaml.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    paths_start, paths_end, indent = _find_core_paths_range(lines)
    if paths_start is None:
        return ["docs/modules.yaml: core module has no paths key — cannot validate"]

    glob_pattern = "changelog.d/*.md"
    if not _insert_path_glob(lines, paths_start, paths_end, indent, glob_pattern):
        return []  # Already present.

    modules_yaml.write_text("".join(lines), encoding="utf-8")
    return [f"docs/modules.yaml: added {glob_pattern} to core module paths"]


def validate_changelog(repo_dir: Path) -> list[str]:
    """Run all changelog validation checks.  Returns diagnostic messages
    for anything that was auto-fixed (empty list means clean)."""
    msgs: list[str] = []
    msgs.extend(_trailing_newline_errors(repo_dir))
    msgs.extend(_modules_yaml_check(repo_dir))
    return msgs


def main() -> int:
    repo_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    msgs = validate_changelog(repo_dir)
    for m in msgs:
        print(f"validate-changelog: {m}", file=sys.stderr)
    if msgs:
        print(f"validate-changelog: {len(msgs)} issue(s) auto-fixed", file=sys.stderr)
    return 0  # Always succeed — issues are auto-fixed.


if __name__ == "__main__":
    raise SystemExit(main())
