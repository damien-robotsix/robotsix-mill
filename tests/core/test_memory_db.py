"""Unit tests for DB-backed memory ledger (load_memory_db / persist_memory_db)."""

from pathlib import Path

import pytest

from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core import models


@pytest.fixture(autouse=True)
def _reset_db():
    """Ensure a clean engine cache for every test."""
    db.reset_engine()


def _make_settings(tmp_path: Path, board_id: str = "test-board") -> Settings:
    """Return a Settings pointed at *tmp_path* so DBs are isolated."""
    return Settings(data_dir=str(tmp_path))


# ── Round-trip ────────────────────────────────────────────────────────────


def test_round_trip(tmp_path: Path):
    """Write content → read back → assert match."""
    s = _make_settings(tmp_path)
    bid, name = "board-a", "refine"

    content = "## Entry 1\n\nSome text.\n\n## Entry 2\n\nMore text.\n"
    db.persist_memory_db(s, bid, name, content)
    result = db.load_memory_db(s, bid, name)
    assert result == content


def test_load_returns_empty_when_no_row(tmp_path: Path):
    """load_memory_db returns '' when no row exists for the key."""
    s = _make_settings(tmp_path)
    result = db.load_memory_db(s, "no-such-board", "no-such-name")
    assert result == ""


def test_persist_overwrites_existing(tmp_path: Path):
    """Second persist replaces the content, does not append."""
    s = _make_settings(tmp_path)
    bid, name = "board-b", "audit"

    db.persist_memory_db(s, bid, name, "first write")
    db.persist_memory_db(s, bid, name, "second write")
    result = db.load_memory_db(s, bid, name)
    assert result == "second write"


def test_different_names_are_independent(tmp_path: Path):
    """Two names on the same board_id do not interfere."""
    s = _make_settings(tmp_path)
    bid = "board-c"

    db.persist_memory_db(s, bid, "refine", "refine content")
    db.persist_memory_db(s, bid, "implement", "implement content")

    assert db.load_memory_db(s, bid, "refine") == "refine content"
    assert db.load_memory_db(s, bid, "implement") == "implement content"


def test_different_boards_are_independent(tmp_path: Path):
    """Two board_ids with the same name do not interfere."""
    s = _make_settings(tmp_path)
    name = "refine"

    db.persist_memory_db(s, "board-1", name, "board-1 content")
    db.persist_memory_db(s, "board-2", name, "board-2 content")

    assert db.load_memory_db(s, "board-1", name) == "board-1 content"
    assert db.load_memory_db(s, "board-2", name) == "board-2 content"


# ── Truncation ────────────────────────────────────────────────────────────


def test_truncation_on_load(tmp_path: Path):
    """Write long content → read with small max_chars → oldest dropped."""
    s = _make_settings(tmp_path)
    bid, name = "board-d", "refine"

    # Build a long chronological ledger with ## entries.
    entries = [f"## Entry {i:04d}\n\nBody of entry {i}.\n" for i in range(500)]
    content = "\n".join(entries)
    db.persist_memory_db(s, bid, name, content)

    # Read with a small cap — should truncate oldest.
    result = db.load_memory_db(s, bid, name, max_chars=3000)
    assert len(result) <= 3500  # small overhead for truncation note
    assert "[... memory (refine) truncated:" in result
    # Most-recent entry survives.
    assert "Entry 0499" in result
    # Oldest entry is gone.
    assert "Entry 0000" not in result


def test_truncation_on_persist(tmp_path: Path):
    """Write long content with small max_chars → truncated before storage."""
    s = _make_settings(tmp_path)
    bid, name = "board-e", "audit"

    entries = [f"## Item {i:04d}\n\nText for item {i}.\n" for i in range(300)]
    content = "\n".join(entries)
    db.persist_memory_db(s, bid, name, content, max_chars=2000)

    stored = db.load_memory_db(s, bid, name)
    # The stored content should already be truncated (no further truncation).
    assert "[... memory (audit) truncated:" in stored
    assert "Item 0299" in stored  # newest
    assert "Item 0000" not in stored  # oldest dropped


def test_truncation_under_limit_no_op(tmp_path: Path):
    """Content shorter than max_chars is stored and returned unchanged."""
    s = _make_settings(tmp_path)
    bid, name = "board-f", "refine"

    content = "Short ledger.\n"
    db.persist_memory_db(s, bid, name, content, max_chars=10_000)
    result = db.load_memory_db(s, bid, name, max_chars=10_000)
    assert result == content


# ── Migration ─────────────────────────────────────────────────────────────


def test_migration_carries_over_legacy_file(tmp_path: Path):
    """When a legacy .md file exists, persist_memory_db migrates it."""
    s = _make_settings(tmp_path)
    bid, name = "board-g", "audit"

    # Create the legacy file at the path memory_file_for resolves to.
    legacy_path = s.memory_file_for(name, bid)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_content = "## Legacy Entry\n\nThis came from a file.\n"
    legacy_path.write_text(legacy_content, encoding="utf-8")

    # First persist with empty text — should pick up legacy content.
    db.persist_memory_db(s, bid, name, "")

    # DB should have the legacy content.
    result = db.load_memory_db(s, bid, name)
    assert result == legacy_content

    # Legacy file should be renamed.
    migrated_path = legacy_path.with_suffix(legacy_path.suffix + ".migrated")
    assert migrated_path.exists()
    assert not legacy_path.exists()


def test_migration_with_new_text_uses_new_text(tmp_path: Path):
    """When legacy file exists but new text is non-empty, use new text."""
    s = _make_settings(tmp_path)
    bid, name = "board-h", "refine"

    legacy_path = s.memory_file_for(name, bid)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text("old legacy content\n", encoding="utf-8")

    new_text = "## Fresh Start\n\nNew agent output.\n"
    db.persist_memory_db(s, bid, name, new_text)

    # DB should have the new text (not legacy).
    result = db.load_memory_db(s, bid, name)
    assert result == new_text

    # Legacy file still renamed.
    migrated_path = legacy_path.with_suffix(legacy_path.suffix + ".migrated")
    assert migrated_path.exists()
    assert not legacy_path.exists()


def test_migration_no_legacy_file_is_clean(tmp_path: Path):
    """When no legacy file exists, first persist works normally."""
    s = _make_settings(tmp_path)
    bid, name = "board-i", "survey"

    # No legacy file — just persist.
    db.persist_memory_db(s, bid, name, "brand new content")
    result = db.load_memory_db(s, bid, name)
    assert result == "brand new content"


def test_migration_already_migrated_does_not_re_migrate(tmp_path: Path):
    """Once a row exists, a legacy file is NOT re-migrated on later persists."""
    s = _make_settings(tmp_path)
    bid, name = "board-j", "health"

    # Create a legacy file AND a migrated copy.
    legacy_path = s.memory_file_for(name, bid)
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text("legacy v1\n", encoding="utf-8")

    # First persist — migrates.
    db.persist_memory_db(s, bid, name, "first persist")
    assert not legacy_path.exists()  # renamed

    # Recreate a file at the same path (simulating a new file that
    # should NOT be picked up since the DB row already exists).
    legacy_path.write_text("legacy v2 — should be ignored\n", encoding="utf-8")

    # Second persist — row already exists, so migration is skipped.
    db.persist_memory_db(s, bid, name, "second persist")
    result = db.load_memory_db(s, bid, name)
    assert result == "second persist"
    # The "new" legacy file should NOT have been renamed (it's not a
    # migration source anymore).
    assert legacy_path.exists()
    # Clean up.
    legacy_path.unlink()


# ── Cross-cutting entries survive retention ───────────────────────────────


def test_cross_cutting_entries_survive_retention(tmp_path: Path):
    """Write entries with ## headers, verify retention keeps recent ones."""
    s = _make_settings(tmp_path)
    bid, name = "board-k", "refine"

    # Build 50 entries, each ~200 chars.
    parts: list[str] = []
    for i in range(50):
        parts.append(f"## Entry {i:02d}\n\n{'x' * 180}\n")
    content = "".join(parts)

    # Persist with a cap that fits ~10 entries.
    # 10 entries × ~200 chars = ~2000 chars; set cap to 2500.
    db.persist_memory_db(s, bid, name, content, max_chars=2500)

    stored = db.load_memory_db(s, bid, name)
    assert "[... memory (refine) truncated:" in stored
    for i in range(40, 50):
        assert f"## Entry {i:02d}" in stored

    # The oldest entries (0-9) should be dropped.
    for i in range(10):
        assert f"## Entry {i:02d}" not in stored


def test_ephemeral_sections_stripped_on_persist(tmp_path: Path):
    """persist_memory_db strips '## Prior proposals' tables before writing."""
    s = _make_settings(tmp_path)
    bid, name = "board-l", "refine"

    content_with_table = """## Cross-cutting pattern

Important observation.

## Prior proposals — verified state

| gap_id | ticket_id | state | resolution |
|--------|-----------|-------|------------|
| abc | abc123 | DRAFT | in-flight |

## More notes

Additional context.
"""
    db.persist_memory_db(s, bid, name, content_with_table)

    stored = db.load_memory_db(s, bid, name)
    # The ephemeral table heading and rows must be gone.
    assert "Prior proposals — verified state" not in stored
    assert "| abc |" not in stored
    # Cross-cutting content survives.
    assert "Important observation" in stored
    assert "Additional context" in stored


def test_ephemeral_proposed_actions_stripped(tmp_path: Path):
    """persist_memory_db strips '## Prior proposed actions — decided' tables."""
    s = _make_settings(tmp_path)
    bid, name = "board-m", "audit"

    content = """## Notes

Pattern seen.

## Prior proposed actions — decided

| id | target_ticket | action | status | decided_by | rationale |
|----|---------------|--------|--------|------------|-----------|
| 1 | abc1234 | close | approved | user | good |

## More patterns

Another note.
"""
    db.persist_memory_db(s, bid, name, content)

    stored = db.load_memory_db(s, bid, name)
    assert "Prior proposed actions — decided" not in stored
    assert "| 1 |" not in stored
    assert "Pattern seen" in stored
    assert "Another note" in stored


def test_recent_proposals_block_stripped(tmp_path: Path):
    """persist_memory_db strips echoed <recent_proposals> blocks."""
    s = _make_settings(tmp_path)
    bid, name = "board-n", "survey"

    content = """## Observations

<recent_proposals>
[DRAFT] abc123 | Some ticket title
[DONE] def456 | Another ticket
</recent_proposals>

Post-block notes.
"""
    db.persist_memory_db(s, bid, name, content)

    stored = db.load_memory_db(s, bid, name)
    assert "<recent_proposals>" not in stored
    assert "abc123" not in stored
    assert "Observations" in stored
    assert "Post-block notes" in stored
