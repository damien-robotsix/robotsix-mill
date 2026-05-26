"""Tests for ci_patterns — structured CI pattern memory."""

import json
import textwrap

import pytest

from robotsix_mill.agents.ci_patterns import (
    CiPatternEntry,
    find_relevant_patterns,
    load_patterns,
    save_patterns,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _entry(
    category="lint_error",
    signature="E501 line too long",
    approach="used edit_file to wrap line",
    success=True,
    attempts=1,
    ticket_id="abc123",
    timestamp="2025-01-01T00:00:00+00:00",
) -> CiPatternEntry:
    return CiPatternEntry(
        category=category,
        signature=signature,
        approach=approach,
        success=success,
        attempts=attempts,
        ticket_id=ticket_id,
        timestamp=timestamp,
    )


# ---------------------------------------------------------------------------
# load_patterns
# ---------------------------------------------------------------------------


def test_load_patterns_returns_empty_on_missing_file(tmp_path):
    path = tmp_path / "nonexistent.json"
    assert load_patterns(path) == []


def test_load_patterns_returns_empty_on_invalid_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not json", encoding="utf-8")
    assert load_patterns(path) == []


def test_load_patterns_roundtrips(tmp_path):
    path = tmp_path / "patterns.json"
    e1 = _entry()
    e2 = _entry(signature="F401 unused import", ticket_id="def456")
    save_patterns(path, [e1, e2])
    loaded = load_patterns(path)
    assert len(loaded) == 2
    assert loaded[0].signature == "E501 line too long"
    assert loaded[0].ticket_id == "abc123"
    assert loaded[1].signature == "F401 unused import"


def test_load_patterns_skips_invalid_entries(tmp_path):
    path = tmp_path / "mixed.json"
    payload = [
        _entry().model_dump(),
        {"garbage": True},
        _entry(signature="ok", ticket_id="t1").model_dump(),
    ]
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = load_patterns(path)
    assert len(loaded) == 2
    sigs = {e.signature for e in loaded}
    assert "E501 line too long" in sigs
    assert "ok" in sigs


# ---------------------------------------------------------------------------
# save_patterns
# ---------------------------------------------------------------------------


def test_save_patterns_trims_to_50(tmp_path):
    path = tmp_path / "many.json"
    entries = [
        _entry(signature=f"E{i:03d}", ticket_id=f"t{i:03d}",
               timestamp=f"2025-01-{(i % 28) + 1:02d}T00:00:00+00:00")
        for i in range(60)
    ]
    save_patterns(path, entries)
    loaded = load_patterns(path)
    assert len(loaded) == 50
    # most recent 50 preserved: first should be entry 10 (index 10 of 0..59)
    assert loaded[0].ticket_id == "t010"


def test_save_patterns_creates_parent_dirs(tmp_path):
    path = tmp_path / "deep" / "nested" / "patterns.json"
    save_patterns(path, [_entry()])
    assert path.exists()
    loaded = load_patterns(path)
    assert len(loaded) == 1


def test_save_patterns_outputs_valid_json(tmp_path):
    path = tmp_path / "valid.json"
    save_patterns(path, [_entry()])
    raw = path.read_text("utf-8")
    parsed = json.loads(raw)
    assert isinstance(parsed, list)
    assert parsed[0]["signature"] == "E501 line too long"


# ---------------------------------------------------------------------------
# find_relevant_patterns
# ---------------------------------------------------------------------------


def test_find_relevant_patterns_substring_match():
    entries = [_entry(signature="E501")]
    result = find_relevant_patterns(entries, "CI failed: E501 line too long")
    assert len(result) == 1


def test_find_relevant_patterns_no_match():
    entries = [_entry(signature="E501")]
    result = find_relevant_patterns(entries, "docker build failed: COPY not found")
    assert len(result) == 0


def test_find_relevant_patterns_category_filter():
    entries = [
        _entry(category="lint_error", signature="E501"),
        _entry(category="docker_build_error", signature="COPY"),
    ]
    result = find_relevant_patterns(
        entries, "E501 line too long and COPY not found",
        category="lint_error",
    )
    assert len(result) == 1
    assert result[0].category == "lint_error"


def test_find_relevant_patterns_limit():
    entries = [
        _entry(signature="E501", ticket_id=f"t{i:03d}",
               timestamp=f"2025-01-{(i % 28) + 1:02d}T00:00:00+00:00")
        for i in range(5)
    ]
    result = find_relevant_patterns(entries, "E501", limit=2)
    assert len(result) == 2


def test_find_relevant_patterns_most_recent_first():
    entries = [
        _entry(signature="E501", ticket_id="old",
               timestamp="2025-01-01T00:00:00+00:00"),
        _entry(signature="E501", ticket_id="new",
               timestamp="2025-02-01T00:00:00+00:00"),
    ]
    result = find_relevant_patterns(entries, "E501 line too long")
    assert len(result) == 2
    assert result[0].ticket_id == "new"
    assert result[1].ticket_id == "old"


def test_find_relevant_patterns_case_insensitive():
    entries = [_entry(signature="e501 LINE TOO long")]
    result = find_relevant_patterns(entries, "E501 line too long")
    assert len(result) == 1


def test_find_relevant_patterns_returns_empty_for_empty_input():
    assert find_relevant_patterns([], "anything") == []
