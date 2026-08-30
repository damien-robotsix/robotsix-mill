"""Unit tests for the per-stage outcome cache (_stage_cache.py)."""

import json

from robotsix_mill.core.states import State
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


def test_blocked_outcome_is_not_written_to_the_cache(tmp_path):
    """A BLOCKED outcome must never be persisted.

    Regression (2026-07-31): caching it made a blocked ticket unrecoverable.
    Ticket …-22ec was resumed after the refine fix written for it had been
    deployed, and the stage logged
    ``refine cache hit (hash=6dce913eed7a…) → blocked`` — replaying the
    pre-fix outcome verbatim without running the fixed code.
    """
    ws = Workspace(tmp_path, "T-1")
    _update(ws, "refine", "abc", Outcome(next_state=State.BLOCKED, note="stuck"))
    assert _load(ws) == {}
    assert _check(ws, "refine", "abc") is None


def test_blocked_outcome_does_not_clobber_an_existing_entry(tmp_path):
    """Declining to cache BLOCKED must not wipe a good prior entry."""
    ws = Workspace(tmp_path, "T-1")
    _update(ws, "refine", "abc", Outcome(next_state=State.READY, note="good"))
    _update(ws, "refine", "abc", Outcome(next_state=State.BLOCKED, note="stuck"))
    result = _check(ws, "refine", "abc")
    assert result is not None
    assert result.next_state == State.READY
    assert result.note == "good"


def test_check_ignores_a_preexisting_blocked_entry(tmp_path):
    """Already-poisoned caches on disk recover without manual deletion.

    Twenty live workspaces held a cached BLOCKED entry when this was found,
    so the read side has to tolerate them rather than only the write side.
    """
    ws = Workspace(tmp_path, "T-1")
    _save(
        ws,
        {"refine": {"input_hash": "abc", "next_state": "blocked", "note": "stuck"}},
    )
    assert _check(ws, "refine", "abc") is None


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


def test_refine_input_hash_is_deterministic(tmp_path):
    ws = Workspace(tmp_path, "T-1")
    ws.write_description("hello world")
    h1 = refine_input_hash(ws)
    h2 = refine_input_hash(ws)
    assert h1 == h2
    assert isinstance(h1, str)
    assert len(h1) == 64  # sha256 hex digest


def test_refine_input_hash_differs_when_description_differs(tmp_path):
    ws = Workspace(tmp_path, "T-1")
    ws.write_description("first")
    h1 = refine_input_hash(ws)
    ws.write_description("second")
    h2 = refine_input_hash(ws)
    assert h1 != h2


def test_refine_input_hash_includes_module_hash(tmp_path):
    """The refine-input hash must include a hash of the refine module
    sources so that a pipeline-code change invalidates the stage cache."""
    from robotsix_mill.stages._stage_cache import _compute_refine_module_hash

    # Force re-compute so we can inspect the value
    mod_hash = _compute_refine_module_hash()
    assert mod_hash and mod_hash != "no-refine-dir"
    assert len(mod_hash) == 64  # sha256 hex digest

    # Verify the module hash is deterministically computed
    mod_hash2 = _compute_refine_module_hash()
    assert mod_hash == mod_hash2

    # Verify the module hash contributes to refine_input_hash output
    ws = Workspace(tmp_path, "T-1")
    ws.write_description("hello")
    h1 = refine_input_hash(ws)

    # If we monkeypatch the cached module hash, the input hash changes
    import robotsix_mill.stages._stage_cache as cache_mod

    saved = cache_mod._REFINE_MODULE_HASH
    try:
        cache_mod._REFINE_MODULE_HASH = "deadbeef"
        h2 = refine_input_hash(ws)
        assert h1 != h2
    finally:
        cache_mod._REFINE_MODULE_HASH = saved


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


def test_review_input_hash_differs_when_the_reviewer_changes(tmp_path, monkeypatch):
    """A change to mill's own reviewer must invalidate cached verdicts.

    Without this the cache keys only what the reviewer reads, never who it
    is, so a reviewer-side fix cannot reach any workspace holding a cached
    outcome — the stale verdict replays and the ticket keeps failing for a
    reason already fixed. Live: central-deploy de52 replayed an 04:06
    REQUEST_CHANGES past the a34839e3 deploy and burned its implement/review
    ceiling a second time without the new reviewer running once.
    """
    from robotsix_mill.agents import reviewing

    ws = Workspace(tmp_path, "T-1")
    ws.write_description("desc")

    monkeypatch.setattr(reviewing, "SYSTEM_PROMPT", "reviewer v1", raising=False)
    h1 = review_input_hash(ws, "diff")

    monkeypatch.setattr(reviewing, "SYSTEM_PROMPT", "reviewer v2", raising=False)
    h2 = review_input_hash(ws, "diff")

    assert h1 != h2, "a changed review prompt must miss the cache"


def test_review_input_hash_differs_on_review_rounds(tmp_path):
    """A different review round (when > 0) must change the cache key.

    Without this a cached REQUEST_CHANGES verdict from round N replays
    in round N+1 even though the reviewer might produce a different
    verdict on the same diff — the implement/review cycle loops until
    the ceiling blocks the ticket.

    Round zero is excluded (see next test) so the reconcile sweep's
    repeated polls over the same un-reviewed state still benefit from
    the cache.
    """
    ws = Workspace(tmp_path, "T-1")
    ws.write_description("desc")
    diff = "--- a/file\n+++ b/file\n@@ -1 +1 @@\n-hello\n+world\n"

    h1 = review_input_hash(ws, diff, review_rounds=1)
    h2 = review_input_hash(ws, diff, review_rounds=2)
    assert h1 != h2, "different review rounds must produce different hashes"


def test_review_input_hash_unchanged_when_rounds_zero(tmp_path):
    """Round zero must not affect the hash — backwards compatibility."""
    ws = Workspace(tmp_path, "T-1")
    ws.write_description("desc")
    diff = "--- a/file\n+++ b/file\n@@ -1 +1 @@\n-hello\n+world\n"

    h_no_rounds = review_input_hash(ws, diff)
    h_rounds_zero = review_input_hash(ws, diff, review_rounds=0)
    assert h_no_rounds == h_rounds_zero, (
        "review_rounds=0 must produce the same hash as the default (no rounds arg)"
    )


def test_cached_request_changes_does_not_replay_across_rounds(tmp_path):
    """A cached REQUEST_CHANGES verdict must not hit when the round changes.

    Regression test for the stale-verdict-replay deadlock.

    Scenario:
    1. Round 0: review runs, cache stores READY outcome (REQUEST_CHANGES)
    2. Ticket resumes into round 1 (review_rounds=1 after implement)
    3. Same diff → the cache key now includes review_rounds=1 → miss
    4. Fresh review runs instead of replaying the stale verdict
    """
    ws = Workspace(tmp_path, "T-1")
    ws.write_description("desc")
    diff = "--- a/file\n+++ b/file\n@@ -1 +1 @@\n-hello\n+world\n"

    # Round 0: simulate a fresh review → REQUEST_CHANGES → cache READY.
    hash_r0 = review_input_hash(ws, diff, review_rounds=0)
    _update(ws, "review", hash_r0, Outcome(next_state=State.READY, note="fix pls"))

    # Round 1: same diff, but review_rounds=1 → different hash → miss.
    hash_r1 = review_input_hash(ws, diff, review_rounds=1)
    assert hash_r1 != hash_r0

    cached = _check(ws, "review", hash_r1)
    assert cached is None, (
        "cached verdict from round 0 must not hit in round 1 — the review must re-run"
    )


def test_reviewer_fingerprint_is_empty_when_it_cannot_be_computed(monkeypatch):
    """An unfingerprintable reviewer must not disable caching entirely."""
    import builtins

    from robotsix_mill.stages import _stage_cache as cache_mod

    real_import = builtins.__import__

    def boom(name, *a, **kw):
        if "reviewing" in name:
            raise RuntimeError("no reviewer")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", boom)
    assert cache_mod.reviewer_fingerprint(None) == ""
