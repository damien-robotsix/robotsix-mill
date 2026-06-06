"""Tests for the deterministic outstanding-TODO scanner.

The scanner replaces the meta-agent's non-deterministic LLM-driven
marker discovery: it must surface every ``TODO``/``FIXME``/``XXX``/
``HACK`` marker in tracked files, case-sensitively, in a stable order,
and never crash on a non-git clone.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from robotsix_mill.meta.todo_scan import (
    MARKERS,
    MAX_PER_REPO,
    TodoMarker,
    format_outstanding_todos,
    scan_outstanding_todos,
)


def _git_repo(path: Path, files: dict[str, str]) -> Path:
    """Init a git repo at *path* and track *files* (rel → content).

    ``git add`` is enough for ``git grep`` to see the files — no commit
    needed. Files created after this call (and never added) stay
    untracked and are invisible to the scan.
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for rel, content in files.items():
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    return path


def test_markers_constant():
    assert MARKERS == ("TODO", "FIXME", "XXX", "HACK")


def test_scan_finds_all_four_markers(tmp_path):
    repo = _git_repo(
        tmp_path / "repo",
        {
            "a.py": "# TODO: foo\n",
            "b.js": "// FIXME bar\n",
            "c.txt": "XXX baz\n",
            "d.md": "HACK qux\n",
        },
    )
    markers = scan_outstanding_todos({"repo-a": repo})
    assert len(markers) == 4
    by_path = {m.path: m for m in markers}

    assert by_path["a.py"].marker == "TODO"
    assert by_path["a.py"].line == 1
    assert by_path["a.py"].text == "TODO: foo"
    assert by_path["a.py"].repo_id == "repo-a"

    assert by_path["b.js"].marker == "FIXME"
    assert by_path["b.js"].text == "FIXME bar"

    assert by_path["c.txt"].marker == "XXX"
    assert by_path["c.txt"].text == "XXX baz"

    assert by_path["d.md"].marker == "HACK"
    assert by_path["d.md"].text == "HACK qux"


def test_lowercase_does_not_match(tmp_path):
    repo = _git_repo(tmp_path / "repo", {"a.py": "x = 1  # todo: lowercase\n"})
    assert scan_outstanding_todos({"repo": repo}) == []


def test_untracked_and_gitignored_files_excluded(tmp_path):
    repo = _git_repo(
        tmp_path / "repo",
        {
            "tracked.py": "# TODO: keep me\n",
            ".gitignore": "ignored.txt\n",
            "ignored.txt": "# TODO: ignored\n",
        },
    )
    # An untracked file created after the initial `git add` is invisible.
    (repo / "untracked.py").write_text("# TODO: untracked\n", encoding="utf-8")

    markers = scan_outstanding_todos({"repo": repo})
    paths = {m.path for m in markers}
    assert paths == {"tracked.py"}


def test_results_sorted_and_stable(tmp_path):
    repo_b = _git_repo(
        tmp_path / "b",
        {"z.py": "# TODO z\n", "a.py": "# TODO a1\n# TODO a2\n"},
    )
    repo_a = _git_repo(tmp_path / "a", {"m.py": "# FIXME m\n"})
    clones = {"repo-b": repo_b, "repo-a": repo_a}

    first = scan_outstanding_todos(clones)
    keys = [(m.repo_id, m.path, m.line) for m in first]
    assert keys == sorted(keys)
    # repo-a sorts before repo-b regardless of dict insertion order.
    assert keys[0][0] == "repo-a"
    # Repeated calls are identical.
    second = scan_outstanding_todos(clones)
    assert [(m.repo_id, m.path, m.line, m.marker, m.text) for m in first] == [
        (m.repo_id, m.path, m.line, m.marker, m.text) for m in second
    ]


def test_caps_truncate_deterministically(tmp_path):
    body = "".join(f"# TODO item {i}\n" for i in range(10))
    repo = _git_repo(tmp_path / "many", {"notes.txt": body})

    capped = scan_outstanding_todos({"many": repo}, max_per_repo=3)
    assert len(capped) == 3
    # Deterministic: the first three by (repo, path, line) are lines 1-3.
    assert [m.line for m in capped] == [1, 2, 3]

    total_capped = scan_outstanding_todos({"many": repo}, max_total=2)
    assert len(total_capped) == 2


def test_format_emits_truncation_note(tmp_path):
    body = "".join(f"# TODO item {i}\n" for i in range(MAX_PER_REPO + 25))
    repo = _git_repo(tmp_path / "many", {"notes.txt": body})

    markers = scan_outstanding_todos({"many": repo})
    assert len(markers) == MAX_PER_REPO

    out = format_outstanding_todos(markers)
    assert str(MAX_PER_REPO) in out
    assert "omitted" in out.lower()


def test_format_groups_and_renders(tmp_path):
    markers = [
        TodoMarker("repo-a", "a.py", 1, "TODO", "TODO: foo"),
        TodoMarker("repo-a", "b.py", 5, "FIXME", "FIXME bar"),
    ]
    out = format_outstanding_todos(markers)
    assert "### `repo-a`" in out
    assert "- `a.py:1` [TODO] TODO: foo" in out
    assert "- `b.py:5` [FIXME] FIXME bar" in out


def test_format_empty_returns_none_found():
    assert format_outstanding_todos([]) == "(none found)"


def test_non_git_clone_is_skipped(tmp_path):
    good = _git_repo(tmp_path / "good", {"a.py": "# TODO: real\n"})
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    (not_a_repo / "a.py").write_text("# TODO: invisible\n", encoding="utf-8")

    markers = scan_outstanding_todos({"good": good, "broken": not_a_repo})
    # The non-git clone is skipped without raising; the good clone is scanned.
    assert {m.repo_id for m in markers} == {"good"}
    assert markers[0].text == "TODO: real"
