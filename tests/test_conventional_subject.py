"""Conventional-commit subjects for mill-authored commits and PRs."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from robotsix_mill.stages._conventional import (
    conventional_subject,
    drop_fragments,
    fragment_kind,
)

TICKET = "20260809T120000Z-do-a-thing-abcd"


def _repo(tmp_path: Path, *, release_please: bool = True) -> Path:
    if release_please:
        (tmp_path / "release-please-config.json").write_text("{}\n")
    return tmp_path


def _fragment(repo: Path, kind: str, ticket: str = TICKET) -> Path:
    d = repo / "changelog.d"
    d.mkdir(exist_ok=True)
    frag = d / f"{ticket}.{kind}.md"
    frag.write_text("Did a thing.\n")
    return frag


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("feature", "feat"),
        ("bugfix", "fix"),
        ("security", "fix"),
        ("removal", "feat"),
        ("deprecation", "feat"),
        ("doc", "docs"),
        ("misc", "chore"),
    ],
)
def test_kind_maps_to_conventional_type(tmp_path, kind, expected):
    repo = _repo(tmp_path)
    _fragment(repo, kind)
    subject = conventional_subject(repo, TICKET, "Do a thing")
    assert subject == f"{expected}: Do a thing ({TICKET})"


def test_ticket_id_stays_in_the_subject(tmp_path):
    """Several stages find a squash-merged branch by grepping for the id."""
    repo = _repo(tmp_path)
    _fragment(repo, "bugfix")
    assert TICKET in conventional_subject(repo, TICKET, "Do a thing")


def test_wip_suffix_is_preserved(tmp_path):
    repo = _repo(tmp_path)
    _fragment(repo, "feature")
    subject = conventional_subject(repo, TICKET, "Do a thing", suffix=" [WIP]")
    assert subject == f"feat: Do a thing ({TICKET}) [WIP]"


def test_missing_fragment_falls_back_to_chore_and_warns(tmp_path, caplog):
    repo = _repo(tmp_path)
    with caplog.at_level(logging.WARNING):
        subject = conventional_subject(repo, TICKET, "Do a thing")
    assert subject.startswith("chore: ")
    assert "no changelog fragment" in caplog.text


def test_unknown_kind_falls_back_and_warns(tmp_path, caplog):
    repo = _repo(tmp_path)
    _fragment(repo, "wibble")
    with caplog.at_level(logging.WARNING):
        subject = conventional_subject(repo, TICKET, "Do a thing")
    assert subject.startswith("chore: ")
    assert "unrecognised changelog kind" in caplog.text


@pytest.mark.parametrize(
    "title",
    [
        "fix: already typed",
        "feat(scope): already typed",
        "feat!: breaking",
        "feat(scope)!: breaking",
    ],
)
def test_an_already_conventional_title_is_not_double_prefixed(tmp_path, title):
    repo = _repo(tmp_path)
    _fragment(repo, "misc")
    subject = conventional_subject(repo, TICKET, title)
    assert subject == f"{title} ({TICKET})"


def test_a_colon_in_prose_is_not_mistaken_for_a_type(tmp_path):
    """'Rebase: improve the guard' must still get a real type."""
    repo = _repo(tmp_path)
    _fragment(repo, "feature")
    subject = conventional_subject(repo, TICKET, "Rebase: improve the drop-guard")
    assert subject == f"feat: Rebase: improve the drop-guard ({TICKET})"


def test_the_most_significant_kind_wins_when_the_agent_wrote_several(tmp_path):
    repo = _repo(tmp_path)
    _fragment(repo, "misc")
    _fragment(repo, "feature")
    _fragment(repo, "bugfix")
    assert fragment_kind(repo, TICKET) == "feature"


def test_another_tickets_fragment_is_ignored(tmp_path):
    repo = _repo(tmp_path)
    _fragment(repo, "feature", ticket="20260809T000000Z-someone-else-9999")
    assert fragment_kind(repo, TICKET) is None


def test_drop_fragments_removes_only_this_ticket(tmp_path):
    repo = _repo(tmp_path)
    mine = _fragment(repo, "feature")
    theirs = _fragment(repo, "bugfix", ticket="20260809T000000Z-someone-else-9999")
    removed = drop_fragments(repo, TICKET)
    assert removed == [mine]
    assert not mine.exists()
    assert theirs.exists()


def test_drop_fragments_removes_the_directory_once_empty(tmp_path):
    repo = _repo(tmp_path)
    _fragment(repo, "feature")
    drop_fragments(repo, TICKET)
    assert not (repo / "changelog.d").exists()


def test_drop_fragments_is_a_noop_on_a_towncrier_repo(tmp_path):
    """A repo that still drains fragments must keep them."""
    repo = _repo(tmp_path, release_please=False)
    frag = _fragment(repo, "feature")
    assert drop_fragments(repo, TICKET) == []
    assert frag.exists()
