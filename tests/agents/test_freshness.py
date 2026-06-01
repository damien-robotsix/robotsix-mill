"""Unit tests for the freshness gate module.

Covers ``extract_cited_paths`` (regex extraction) and
``run_freshness_check`` (file-verification logic) independently
from the refine-stage integration.
"""

import tempfile
from pathlib import Path

import pytest

from robotsix_mill.agents.freshness import extract_cited_paths, run_freshness_check


# --- extract_cited_paths ---


def test_extract_backtick_quoted_paths():
    """Backtick-quoted file paths are extracted."""
    draft = "Fix `src/foo.py` and `docs/bar.md`."
    paths = extract_cited_paths(draft)
    assert "src/foo.py" in paths
    assert "docs/bar.md" in paths


def test_extract_backtick_quoted_with_line_range():
    """Backtick-quoted paths with line ranges are extracted."""
    draft = "See `src/foo.py:42` and `src/bar.py:10-20`."
    paths = extract_cited_paths(draft)
    assert "src/foo.py:42" in paths
    assert "src/bar.py:10-20" in paths


def test_extract_bare_paths():
    """Bare (non-backtick) file paths are extracted."""
    draft = "The problem is in src/models.py and tests/test_foo.py."
    paths = extract_cited_paths(draft)
    assert "src/models.py" in paths
    assert "tests/test_foo.py" in paths


def test_extract_deduplicates():
    """The same path cited multiple times is only returned once."""
    draft = "Fix `src/foo.py` and also src/foo.py again."
    paths = extract_cited_paths(draft)
    assert paths.count("src/foo.py") == 1


def test_extract_ignores_non_file_backticks():
    """Backtick-quoted strings that don't look like file paths are ignored."""
    draft = "The `foo` function in `bar` class."
    paths = extract_cited_paths(draft)
    assert paths == []


def test_extract_requires_directory_separator():
    """Single-word strings ending in .py without a / are not file paths."""
    draft = "The file.py module is here."
    paths = extract_cited_paths(draft)
    # "file.py" has no directory separator → not a path citation
    assert "file.py" not in paths


def test_extract_no_citations():
    """Draft with no file citations returns empty list."""
    draft = "Just some prose with no paths."
    paths = extract_cited_paths(draft)
    assert paths == []


# --- run_freshness_check ---


def test_run_freshness_no_repo():
    """When repo_dir is None, staleness check is skipped."""
    result = run_freshness_check(
        draft="Fix `src/foo.py`, `src/bar.py`, `src/baz.py`.",
        repo_dir=None,
    )
    assert result["stale"] is False
    assert "no repo" in result["reason"]


def test_run_freshness_too_few_citations():
    """Fewer than 3 cited paths → insufficient for staleness call."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        result = run_freshness_check(
            draft="Fix `src/foo.py` and `src/bar.py`.",
            repo_dir=repo,
        )
        assert result["stale"] is False
        assert "2 cited" in result["reason"]


def test_run_freshness_all_exist():
    """All cited files exist → not stale."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "src").mkdir(parents=True, exist_ok=True)
        (repo / "src" / "foo.py").write_text("x")
        (repo / "src" / "bar.py").write_text("y")
        (repo / "src" / "baz.py").write_text("z")

        result = run_freshness_check(
            draft="Fix `src/foo.py`, `src/bar.py`, `src/baz.py`.",
            repo_dir=repo,
        )
        assert result["stale"] is False
        assert "3/3" in result["reason"]


def test_run_freshness_all_missing():
    """All cited files missing → stale (hallucinated finding)."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        result = run_freshness_check(
            draft=(
                "The following files are missing:\n"
                "- `docs/api.md`\n"
                "- `docs/guide.md`\n"
                "- `docs/reference.md`\n"
            ),
            repo_dir=repo,
        )
        assert result["stale"] is True
        assert "none of 3" in result["reason"]


def test_run_freshness_most_missing():
    """Most cited files missing (≥5 cited, <33% exist) → stale."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "src").mkdir(parents=True, exist_ok=True)
        (repo / "src" / "only_this_exists.py").write_text("x")

        result = run_freshness_check(
            draft=(
                "Issues found in:\n"
                "- `src/only_this_exists.py`\n"
                "- `src/missing1.py`\n"
                "- `src/missing2.py`\n"
                "- `src/missing3.py`\n"
                "- `src/missing4.py`\n"
            ),
            repo_dir=repo,
        )
        assert result["stale"] is True
        assert "1/5" in result["reason"]


def test_run_freshness_line_range_within_bounds():
    """Cited line range within file bounds → not stale."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "src").mkdir(parents=True, exist_ok=True)
        (repo / "src" / "foo.py").write_text("line1\nline2\nline3\nline4\nline5\n")
        (repo / "src" / "bar.py").write_text("a\nb\nc\n")
        (repo / "src" / "baz.py").write_text("x\ny\nz\n")

        result = run_freshness_check(
            draft="Fix `src/foo.py:3`, `src/bar.py:2`, `src/baz.py:1`.",
            repo_dir=repo,
        )
        assert result["stale"] is False
        assert "3/3" in result["reason"]


def test_run_freshness_line_range_beyond_eof():
    """Cited line range beyond EOF → file treated as missing."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / "src").mkdir(parents=True, exist_ok=True)
        # Only 3 lines but cited line 42.
        (repo / "src" / "foo.py").write_text("a\nb\nc\n")
        (repo / "src" / "bar.py").write_text("x\n")
        (repo / "src" / "baz.py").write_text("1\n")

        result = run_freshness_check(
            draft="Fix `src/foo.py:42`, `src/bar.py:99`, `src/baz.py:50`.",
            repo_dir=repo,
        )
        # All three files exist but line ranges are beyond EOF → stale.
        assert result["stale"] is True
        assert "none of 3" in result["reason"]
