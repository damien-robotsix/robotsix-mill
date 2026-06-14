"""Regression test guarding against orphan docs.

Every ``docs/**/*.md`` file must be reachable from the MkDocs site,
i.e. it must be referenced somewhere in the ``nav:`` tree of
``mkdocs.yml``.  Mirrors the style of ``tests/test_modules_yaml_paths.py``
(validate that a manifest's declared paths match on-disk reality).

If a doc legitimately should not appear in ``nav`` (e.g. a partial or
include), add its repo-relative path to ``_IGNORE``.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DOCS_DIR = _REPO_ROOT / "docs"
_MKDOCS_YML = _REPO_ROOT / "mkdocs.yml"

# Docs deliberately excluded from nav (partials/includes). Paths are
# relative to the docs/ directory, matching how nav references them.
_IGNORE: frozenset[str] = frozenset()


def _collect_nav_doc_paths(nav: object) -> set[str]:
    """Recursively collect every ``.md`` path referenced in a nav tree."""

    found: set[str] = set()
    if isinstance(nav, str):
        if nav.endswith(".md"):
            found.add(nav)
    elif isinstance(nav, list):
        for item in nav:
            found |= _collect_nav_doc_paths(item)
    elif isinstance(nav, dict):
        for value in nav.values():
            found |= _collect_nav_doc_paths(value)
    return found


def _on_disk_docs() -> set[str]:
    return {md.relative_to(_DOCS_DIR).as_posix() for md in _DOCS_DIR.rglob("*.md")}


def test_mkdocs_yml_exists() -> None:
    assert _MKDOCS_YML.is_file(), "mkdocs.yml not found at repo root"


def test_no_orphan_docs() -> None:
    config = yaml.safe_load(_MKDOCS_YML.read_text())
    nav_paths = _collect_nav_doc_paths(config.get("nav"))

    disk_docs = _on_disk_docs() - _IGNORE
    orphans = sorted(disk_docs - nav_paths)

    assert not orphans, (
        "These docs/ files are not referenced anywhere in the mkdocs.yml "
        f"nav tree (orphaned from the built site): {orphans}. Add a nav "
        "entry for each, or declare it in _IGNORE if intentionally excluded."
    )


def test_nav_targets_exist() -> None:
    config = yaml.safe_load(_MKDOCS_YML.read_text())
    nav_paths = _collect_nav_doc_paths(config.get("nav"))

    missing = sorted(p for p in nav_paths if not (_DOCS_DIR / p).is_file())
    assert not missing, f"nav references docs that do not exist on disk: {missing}"
