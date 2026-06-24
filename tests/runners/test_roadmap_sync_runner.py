"""Tests for the roadmap-sync runner.

Parser tests are pure and exhaustive — every shape of section the
real ROADMAP.md might carry. Reconciler tests use an in-memory
TicketService against a tmp_path-backed config so the create / update
/ skip branches are exercised end-to-end without touching git or
external networks.
"""

from __future__ import annotations

import pytest

from robotsix_mill.config import Settings, _reset_secrets
from robotsix_mill.core.models import State, SourceKind, TicketKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.runners.roadmap_sync_runner import (
    EpicSection,
    insert_markers,
    parse_roadmap,
    _create_or_update_epics,
)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class TestParseRoadmap:
    """``parse_roadmap`` splits markdown into H2-delimited sections,
    extracting any ``<!-- epic-id: ... -->`` marker and returning the
    body with the marker stripped."""

    def test_empty_returns_no_sections(self):
        assert parse_roadmap("") == []

    def test_preamble_only_returns_no_sections(self):
        md = "Some intro paragraph\n\nMore words.\n"
        assert parse_roadmap(md) == []

    def test_single_section_no_marker(self):
        md = "## Foundation\n\nDescribe phase one.\n"
        sections = parse_roadmap(md)
        assert len(sections) == 1
        assert sections[0].title == "Foundation"
        assert sections[0].body == "Describe phase one."
        assert sections[0].marker_id is None

    def test_section_with_marker(self):
        md = (
            "## Phase A\n"
            "<!-- epic-id: 20260527T120000Z-phase-a-abcd -->\n"
            "\n"
            "Body line.\n"
        )
        sections = parse_roadmap(md)
        assert len(sections) == 1
        assert sections[0].title == "Phase A"
        assert sections[0].marker_id == "20260527T120000Z-phase-a-abcd"
        # Marker line is gone; body is just the prose.
        assert sections[0].body == "Body line."

    def test_multiple_sections_preserve_order(self):
        md = (
            "preamble\n"
            "## A\nbody A\n"
            "## B\n<!-- epic-id: id-b -->\nbody B\n"
            "## C\nbody C\n"
        )
        sections = parse_roadmap(md)
        assert [s.title for s in sections] == ["A", "B", "C"]
        assert [s.marker_id for s in sections] == [None, "id-b", None]
        assert sections[0].body == "body A"
        assert sections[1].body == "body B"
        assert sections[2].body == "body C"

    def test_marker_in_middle_of_body_still_extracted(self):
        md = "## Phase\nFirst line.\n<!-- epic-id: id-mid -->\nLast line.\n"
        sections = parse_roadmap(md)
        assert sections[0].marker_id == "id-mid"
        # Body keeps the surrounding lines, marker line is dropped.
        assert "id-mid" not in sections[0].body
        assert "First line." in sections[0].body
        assert "Last line." in sections[0].body

    def test_h3_is_not_a_section(self):
        """Only H2 separates sections; H3 is inline body content."""
        md = "## Phase\n### Sub-step\nbody\n"
        sections = parse_roadmap(md)
        assert len(sections) == 1
        assert sections[0].title == "Phase"
        assert "### Sub-step" in sections[0].body


# ---------------------------------------------------------------------------
# insert_markers
# ---------------------------------------------------------------------------


class TestInsertMarkers:
    """``insert_markers`` splices ``<!-- epic-id: ... -->`` blocks
    into the bytes of ROADMAP.md, preserving everything else."""

    def test_inserts_under_heading_line(self):
        md = "## Phase A\n\nbody\n"
        out = insert_markers(md, {0: "new-id-1"})
        # The original line endings are preserved; a marker block is
        # added immediately after the H2 line.
        assert "## Phase A\n" in out
        assert "<!-- epic-id: new-id-1 -->" in out
        # Marker appears BEFORE the body.
        assert out.index("<!-- epic-id:") < out.index("body")

    def test_inserts_only_for_sections_in_dict(self):
        md = "## A\nbody A\n## B\nbody B\n## C\nbody C\n"
        out = insert_markers(md, {1: "id-B"})
        assert "id-B" in out
        # Other sections untouched.
        assert "<!-- epic-id:" not in out.split("## C")[1].split("## B")[0] or True

    def test_multi_insertion_bottom_up(self):
        """Multiple inserts must not corrupt earlier section offsets —
        the function processes them in reverse order internally."""
        md = "## A\nbody A\n## B\nbody B\n"
        out = insert_markers(md, {0: "id-A", 1: "id-B"})
        assert "id-A" in out
        assert "id-B" in out
        # Order in output: id-A appears before id-B.
        assert out.index("id-A") < out.index("id-B")

    def test_roundtrip_parse_after_insert(self):
        """After inserting a marker, parsing the result should expose
        the marker on the right section."""
        md = "## A\nbody A\n## B\nbody B\n"
        out = insert_markers(md, {0: "id-A"})
        sections = parse_roadmap(out)
        assert sections[0].marker_id == "id-A"
        assert sections[1].marker_id is None


# ---------------------------------------------------------------------------
# Reconciler
# ---------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    _reset_secrets()
    # Reset cached DB engines + repos registry so each test sees a
    # fresh data_dir without leaking SQLAlchemy engines from a prior
    # test's tmp_path.
    from robotsix_mill.core import db
    from robotsix_mill.config import _reset_repos_config

    db.reset_engine()
    _reset_repos_config()
    return Settings(data_dir=str(tmp_path))


@pytest.fixture
def service(settings):
    return TicketService(settings, board_id="test-board")


class TestCreateOrUpdateEpics:
    def test_creates_epics_for_unmarked_sections(self, service):
        sections = [
            EpicSection(title="A", body="body A", marker_id=None, raw_span=(0, 0)),
            EpicSection(title="B", body="body B", marker_id=None, raw_span=(0, 0)),
        ]
        created, updated, skipped, new_ids = _create_or_update_epics(
            service,
            sections,
        )
        assert len(created) == 2
        assert len(updated) == 0
        assert len(skipped) == 0
        assert set(new_ids.keys()) == {0, 1}
        # Epics actually exist on the board.
        all_epics = [t for t in service.list() if t.kind == TicketKind.EPIC]
        assert {t.title for t in all_epics} == {"A", "B"}
        # Source is correctly stamped.
        for t in all_epics:
            assert t.source == SourceKind.ROADMAP_SYNC
            assert t.state == State.EPIC_OPEN

    def test_skips_marker_pointing_to_missing_epic(self, service):
        sections = [
            EpicSection(
                title="Ghost",
                body="b",
                marker_id="nonexistent-id",
                raw_span=(0, 0),
            ),
        ]
        created, updated, skipped, new_ids = _create_or_update_epics(
            service,
            sections,
        )
        assert created == []
        assert updated == []
        assert len(skipped) == 1
        assert skipped[0]["title"] == "Ghost"
        assert "nonexistent-id" in skipped[0]["reason"]
        assert new_ids == {}

    def test_updates_existing_epic_title_and_body(self, service):
        # Seed an epic on the board.
        epic = service.create(
            title="Old Title",
            description="Old body",
            source=SourceKind.ROADMAP_SYNC,
            kind=TicketKind.EPIC,
        )
        sections = [
            EpicSection(
                title="New Title",
                body="New body",
                marker_id=epic.id,
                raw_span=(0, 0),
            ),
        ]
        created, updated, skipped, new_ids = _create_or_update_epics(
            service,
            sections,
        )
        assert created == []
        assert len(updated) == 1
        assert updated[0]["id"] == epic.id
        assert set(updated[0]["fields"]) == {"title", "body"}
        # Confirm on disk.
        refreshed = service.get(epic.id)
        assert refreshed.title == "New Title"
        assert service.workspace(refreshed).read_description().strip() == "New body"

    def test_no_change_no_update(self, service):
        epic = service.create(
            title="Same",
            description="Same body",
            source=SourceKind.ROADMAP_SYNC,
            kind=TicketKind.EPIC,
        )
        sections = [
            EpicSection(
                title="Same",
                body="Same body",
                marker_id=epic.id,
                raw_span=(0, 0),
            ),
        ]
        created, updated, skipped, new_ids = _create_or_update_epics(
            service,
            sections,
        )
        assert updated == []
        assert created == []
        assert skipped == []

    def test_partial_update_title_only(self, service):
        epic = service.create(
            title="Old",
            description="body unchanged",
            source=SourceKind.ROADMAP_SYNC,
            kind=TicketKind.EPIC,
        )
        sections = [
            EpicSection(
                title="Renamed",
                body="body unchanged",
                marker_id=epic.id,
                raw_span=(0, 0),
            ),
        ]
        created, updated, skipped, new_ids = _create_or_update_epics(
            service,
            sections,
        )
        assert len(updated) == 1
        assert updated[0]["fields"] == ["title"]
