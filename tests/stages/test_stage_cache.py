"""Unit tests for the per-stage outcome cache (_stage_cache.py)."""

import json

from robotsix_mill.core.workspace import Workspace
from robotsix_mill.stages._stage_cache import (
    _cache_path,
    _check,
    _load,
    _save,
    _update,
    refine_input_hash,
    review_input_hash,
)
from robotsix_mill.stages.base import Outcome
from robotsix_mill.core.states import State


# ---------------------------------------------------------------------------
# _cache_path
# ---------------------------------------------------------------------------


def test_cache_path_returns_artifacts_dir_plus_filename(tmp_path):
    ws = Workspace(tmp_path, "T-1")
    p = _cache_path(ws)
    assert p.name == "stage_cache.json"
    assert p.parent == ws.artifacts_dir


# ---------------------------------------------------------------------------
# _load
# ---------------------------------------------------------------------------


def test_load_returns_empty_dict_when_no_cache_file(tmp_path):
    ws = Workspace(tmp_path, "T-1")
    assert _load(ws) == {}


def test_load_returns_parsed_json_when_file_exists(tmp_path):
    ws = Workspace(tmp_path, "T-1")
    data = {"refine": {"input_hash": "abc", "next_state": "ready", "note": ""}}
    _cache_path(ws).write_text(json.dumps(data), encoding="utf-8")
    assert _load(ws) == data


def test_load_returns_empty_dict_on_corrupt_json(tmp_path):
    ws = Workspace(tmp_path, "T-1")
    _cache_path(ws).parent.mkdir(parents=True, exist_ok=True)
    _cache_path(ws).write_text("not valid json {{{", encoding="utf-8")
    assert _load(ws) == {}


# ---------------------------------------------------------------------------
# _save
# ---------------------------------------------------------------------------


def test_save_writes_json_to_cache_path(tmp_path):
    ws = Workspace(tmp_path, "T-1")
    data = {"review": {"input_hash": "xyz", "next_state": "deliverable", "note": "ok"}}
    _save(ws, data)
    assert json.loads(_cache_path(ws).read_text(encoding="utf-8")) == data


def test_save_creates_parent_dir(tmp_path):
    ws = Workspace(tmp_path, "T-1")
    # Remove the artifacts dir that Workspace creates lazily (if any).
    # _save calls mkdir, so even a bare dir without artifacts/ should work.
    ws2 = Workspace(ws.dir / "deeper", "sub")
    data = {"k": "v"}
    _save(ws2, data)
    assert json.loads(_cache_path(ws2).read_text(encoding="utf-8")) == data


# ---------------------------------------------------------------------------
# _check
# ---------------------------------------------------------------------------


def test_check_returns_none_when_cache_empty(tmp_path):
    ws = Workspace(tmp_path, "T-1")
    assert _check(ws, "refine", "somehash") is None


def test_check_returns_none_when_stage_not_in_cache(tmp_path):
    ws = Workspace(tmp_path, "T-1")
    _update(ws, "other", "h", Outcome(next_state=State.READY))
    assert _check(ws, "refine", "h") is None


def test_check_returns_none_when_hash_mismatch(tmp_path):
    ws = Workspace(tmp_path, "T-1")
    _update(ws, "refine", "oldhash", Outcome(next_state=State.READY))
    assert _check(ws, "refine", "newhash") is None


def test_check_returns_outcome_on_hash_match(tmp_path):
    ws = Workspace(tmp_path, "T-1")
    _update(ws, "refine", "abc", Outcome(next_state=State.READY, note="cached"))
    result = _check(ws, "refine", "abc")
    assert result is not None
    assert result.next_state == State.READY
    assert result.note == "cached"


def test_check_returns_none_when_entry_has_no_next_state(tmp_path):
    ws = Workspace(tmp_path, "T-1")
    _save(ws, {"refine": {"input_hash": "abc", "note": ""}})
    assert _check(ws, "refine", "abc") is None


def test_check_returns_none_when_next_state_is_invalid(tmp_path):
    ws = Workspace(tmp_path, "T-1")
    _save(
        ws, {"refine": {"input_hash": "abc", "next_state": "bogus_state", "note": ""}}
    )
    assert _check(ws, "refine", "abc") is None


# ---------------------------------------------------------------------------
# _update
# ---------------------------------------------------------------------------


def test_update_stores_entry_under_stage_name(tmp_path):
    ws = Workspace(tmp_path, "T-1")
    _update(
        ws, "review", "hash1", Outcome(next_state=State.CODE_REVIEW, note="looks good")
    )
    raw = json.loads(_cache_path(ws).read_text(encoding="utf-8"))
    assert "review" in raw
    assert raw["review"]["input_hash"] == "hash1"
    assert raw["review"]["next_state"] == "code_review"
    assert raw["review"]["note"] == "looks good"


def test_update_overwrites_existing_entry(tmp_path):
    ws = Workspace(tmp_path, "T-1")
    _update(ws, "refine", "h1", Outcome(next_state=State.READY))
    _update(ws, "refine", "h2", Outcome(next_state=State.DRAFT))
    raw = json.loads(_cache_path(ws).read_text(encoding="utf-8"))
    assert len(raw) == 1
    assert raw["refine"]["input_hash"] == "h2"
    assert raw["refine"]["next_state"] == "draft"


def test_update_preserves_other_entries(tmp_path):
    ws = Workspace(tmp_path, "T-1")
    _update(ws, "refine", "h1", Outcome(next_state=State.READY))
    _update(ws, "review", "h2", Outcome(next_state=State.CODE_REVIEW))
    raw = json.loads(_cache_path(ws).read_text(encoding="utf-8"))
    assert "refine" in raw
    assert "review" in raw


def test_update_handles_note_none(tmp_path):
    ws = Workspace(tmp_path, "T-1")
    _update(ws, "refine", "h", Outcome(next_state=State.READY))
    raw = json.loads(_cache_path(ws).read_text(encoding="utf-8"))
    assert raw["refine"]["note"] == ""


# ---------------------------------------------------------------------------
# refine_input_hash
# ---------------------------------------------------------------------------


def test_refine_input_hash_delegates_to_content_hash(tmp_path):
    ws = Workspace(tmp_path, "T-1")
    ws.write_description("hello world")
    expected = ws.content_hash()
    assert refine_input_hash(ws) == expected


def test_refine_input_hash_differs_when_description_differs(tmp_path):
    ws = Workspace(tmp_path, "T-1")
    ws.write_description("first")
    h1 = refine_input_hash(ws)
    ws.write_description("second")
    h2 = refine_input_hash(ws)
    assert h1 != h2


# ---------------------------------------------------------------------------
# review_input_hash
# ---------------------------------------------------------------------------


def test_review_input_hash_is_deterministic(tmp_path):
    ws = Workspace(tmp_path, "T-1")
    ws.write_description("desc")
    diff = "--- a/file\n+++ b/file\n@@ -1 +1 @@\n-hello\n+world\n"
    h1 = review_input_hash(ws, diff)
    h2 = review_input_hash(ws, diff)
    assert h1 == h2
    assert isinstance(h1, str)
    assert len(h1) == 64  # sha256 hex digest


def test_review_input_hash_differs_on_diff_change(tmp_path):
    ws = Workspace(tmp_path, "T-1")
    ws.write_description("desc")
    h1 = review_input_hash(ws, "diff A")
    h2 = review_input_hash(ws, "diff B")
    assert h1 != h2


def test_review_input_hash_differs_on_description_change(tmp_path):
    ws = Workspace(tmp_path, "T-1")
    ws.write_description("desc A")
    h1 = review_input_hash(ws, "diff")
    ws.write_description("desc B")
    h2 = review_input_hash(ws, "diff")
    assert h1 != h2
