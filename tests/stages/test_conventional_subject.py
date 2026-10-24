"""Conventional-commit subjects for mill-authored commits and PRs."""

from __future__ import annotations

import logging

import pytest

from robotsix_mill.stages._conventional import (
    conventional_subject,
    record_type,
    type_from_summary,
)

TICKET = "20260809T120000Z-do-a-thing-abcd"


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        ("Did the thing.\n\nChange-Type: fix\n", "fix"),
        ("Change-Type: feat", "feat"),
        ("- **Change-Type:** `docs`", "docs"),
        ("change type: Refactor", "refactor"),
        ("Change_Type: test", "test"),
    ],
)
def test_type_from_summary_reads_the_declared_type(summary, expected):
    assert type_from_summary(summary) == expected


def test_type_from_summary_ignores_prose_and_unknown_types(caplog):
    assert type_from_summary(None) is None
    assert type_from_summary("") is None
    assert type_from_summary("I changed the type of the field.") is None
    with caplog.at_level(logging.WARNING):
        assert type_from_summary("Change-Type: wibble") is None
    assert "unrecognised Change-Type" in caplog.text


def test_recorded_type_drives_the_subject(tmp_path):
    artifacts = tmp_path / "artifacts"
    record_type(artifacts, type_from_summary("Change-Type: feat"))
    subject = conventional_subject(TICKET, "Do a thing", artifacts_dir=artifacts)
    assert subject == f"feat: Do a thing ({TICKET})"


def test_ticket_id_stays_in_the_subject(tmp_path):
    """Several stages find a squash-merged branch by grepping for the id."""
    record_type(tmp_path, "fix")
    assert TICKET in conventional_subject(TICKET, "Do a thing", artifacts_dir=tmp_path)


def test_wip_suffix_is_preserved(tmp_path):
    record_type(tmp_path, "feat")
    subject = conventional_subject(
        TICKET, "Do a thing", suffix=" [WIP]", artifacts_dir=tmp_path
    )
    assert subject == f"feat: Do a thing ({TICKET}) [WIP]"


def test_missing_type_falls_back_to_chore_and_warns(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        subject = conventional_subject(
            TICKET, "Do a thing", artifacts_dir=tmp_path / "nope"
        )
    assert subject.startswith("chore: ")
    assert "no Change-Type recorded" in caplog.text


def test_no_artifacts_dir_at_all_still_falls_back():
    assert conventional_subject(TICKET, "Do a thing").startswith("chore: ")


def test_a_corrupt_recorded_type_falls_back(tmp_path):
    (tmp_path / "change_type.txt").write_text("wibble\n")
    assert conventional_subject(
        TICKET, "Do a thing", artifacts_dir=tmp_path
    ).startswith("chore: ")


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
    record_type(tmp_path, "chore")
    subject = conventional_subject(TICKET, title, artifacts_dir=tmp_path)
    assert subject == f"{title} ({TICKET})"


def test_a_colon_in_prose_is_not_mistaken_for_a_type(tmp_path):
    """'Rebase: improve the guard' must still get a real type."""
    record_type(tmp_path, "feat")
    subject = conventional_subject(
        TICKET, "Rebase: improve the drop-guard", artifacts_dir=tmp_path
    )
    assert subject == f"feat: Rebase: improve the drop-guard ({TICKET})"


def test_deliver_classifies_from_what_implement_parked(tmp_path):
    """implement records the type; deliver runs later from the artifact alone.

    Without the parked type every PR title falls back to `chore`, and for a
    multi-commit PR GitHub squashes under that title — so the real type is
    lost exactly where release-please reads it.
    """
    artifacts = tmp_path / "artifacts"
    record_type(artifacts, type_from_summary("All done.\nChange-Type: fix"))
    assert (
        conventional_subject(TICKET, "Do a thing", artifacts_dir=artifacts)
        == f"fix: Do a thing ({TICKET})"
    )


def test_record_type_tolerates_a_missing_dir_and_a_none_type(tmp_path):
    record_type(None, "feat")  # no artifacts dir
    record_type(tmp_path / "new", None)  # nothing to record
    assert not (tmp_path / "new" / "change_type.txt").exists()
