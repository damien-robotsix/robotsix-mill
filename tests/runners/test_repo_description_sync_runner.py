"""Tests for the repo-description-sync runner utility functions."""

from __future__ import annotations

from pathlib import Path

import pytest

from robotsix_mill.runners.repo_description_sync_runner import (
    _extract_h1_and_first_paragraph,
    _find_readme,
    _parse_owner_repo,
    RepoDescriptionSyncPassResult,
)


# ---------------------------------------------------------------------------
# _find_readme
# ---------------------------------------------------------------------------


def test_find_readme_md(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Hello\n", encoding="utf-8")
    assert _find_readme(tmp_path) == tmp_path / "README.md"


def test_find_readme_rst(tmp_path: Path) -> None:
    (tmp_path / "README.rst").write_text("Hello\n=====\n", encoding="utf-8")
    assert _find_readme(tmp_path) == tmp_path / "README.rst"


def test_find_readme_plain(tmp_path: Path) -> None:
    (tmp_path / "README").write_text("Hello\n", encoding="utf-8")
    assert _find_readme(tmp_path) == tmp_path / "README"


def test_find_readme_lowercase(tmp_path: Path) -> None:
    (tmp_path / "readme.md").write_text("# Hello\n", encoding="utf-8")
    assert _find_readme(tmp_path) == tmp_path / "readme.md"


def test_find_readme_none(tmp_path: Path) -> None:
    assert _find_readme(tmp_path) is None


def test_find_readme_prefers_first_match(tmp_path: Path) -> None:
    # README.md should be found before README.rst
    (tmp_path / "README.md").write_text("# md\n", encoding="utf-8")
    (tmp_path / "README.rst").write_text("rst\n====\n", encoding="utf-8")
    assert _find_readme(tmp_path) == tmp_path / "README.md"


# ---------------------------------------------------------------------------
# _extract_h1_and_first_paragraph
# ---------------------------------------------------------------------------


def test_extract_simple() -> None:
    h1, para = _extract_h1_and_first_paragraph(
        "# My Project\n\nThis is a description.\n"
    )
    assert h1 == "My Project"
    assert para == "This is a description."


def test_extract_no_paragraph() -> None:
    h1, para = _extract_h1_and_first_paragraph("# Only heading\n")
    assert h1 == "Only heading"
    assert para == ""


def test_extract_no_h1() -> None:
    h1, para = _extract_h1_and_first_paragraph("Just some text\nNo heading here.\n")
    assert h1 == ""
    assert para == ""


def test_extract_skips_subheadings() -> None:
    h1, para = _extract_h1_and_first_paragraph(
        "# Top Level\n\n## Subheading\n\nFirst real paragraph.\n"
    )
    assert h1 == "Top Level"
    assert para == "First real paragraph."


def test_extract_empty_string() -> None:
    h1, para = _extract_h1_and_first_paragraph("")
    assert h1 == ""
    assert para == ""


def test_extract_h1_with_leading_whitespace() -> None:
    h1, para = _extract_h1_and_first_paragraph(
        "   # Padded Heading   \n\nPadded paragraph.   \n"
    )
    assert h1 == "Padded Heading"
    assert para == "Padded paragraph."


def test_extract_blank_lines_before_paragraph() -> None:
    h1, para = _extract_h1_and_first_paragraph("# H1\n\n\n\n\nFinally a paragraph.\n")
    assert h1 == "H1"
    assert para == "Finally a paragraph."


# ---------------------------------------------------------------------------
# _parse_owner_repo
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/owner/repo.git", ("owner", "repo")),
        ("https://github.com/owner/repo", ("owner", "repo")),
        ("https://gitlab.com/group/subgroup/repo.git", ("group/subgroup", "repo")),
        ("git@github.com:owner/repo.git", ("owner", "repo")),
        ("git@gitlab.com:namespace/project.git", ("namespace", "project")),
        ("https://git.example.com/team/project", ("team", "project")),
    ],
)
def test_parse_owner_repo_valid(url: str, expected: tuple[str, str]) -> None:
    assert _parse_owner_repo(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not-a-url",
        "https://github.com/owner",  # missing repo
        "git@github.com:owner",  # missing repo
    ],
)
def test_parse_owner_repo_invalid_raises(url: str) -> None:
    with pytest.raises(ValueError, match="cannot parse owner/repo"):
        _parse_owner_repo(url)


# ---------------------------------------------------------------------------
# RepoDescriptionSyncPassResult
# ---------------------------------------------------------------------------


def test_result_defaults() -> None:
    r = RepoDescriptionSyncPassResult()
    assert r.updated_memory == ""
    assert r.drafts_created == []
    assert r.session_id == ""
    assert r.summary == ""
    assert r.description_updated is False


def test_result_fields() -> None:
    r = RepoDescriptionSyncPassResult(
        updated_memory="mem",
        drafts_created=[{"id": "1"}],
        session_id="sid",
        summary="ok",
        description_updated=True,
    )
    assert r.description_updated is True
    assert r.summary == "ok"
