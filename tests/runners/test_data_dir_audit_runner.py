"""Tests for the data-dir audit runner.

Covers:

- Top-N largest items (ticket 2): ``find_largest_items``.
- Growth-delta tracking (ticket 3): state I/O helpers
  (``_growth_state_path``, ``_load_growth_state``,
  ``_save_growth_state``), board enumeration, size scanning with
  cumulative directory sizes (no double-counting), delta computation
  with byte/pct/both thresholds, and integration through
  ``run_data_dir_audit_pass``.
- Unbounded-collection candidate detection (ticket 4):
  ``check_unbounded_candidates`` and its integration into
  ``run_data_dir_audit_pass``.
- Orphan-workspace detection (ticket 5): ``find_orphan_workspaces``
  plus its integration into ``run_data_dir_audit_pass``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from robotsix_mill.config import Settings, _reset_secrets
from robotsix_mill.core import db
from robotsix_mill.core.models import Ticket
from robotsix_mill.core.states import State
from robotsix_mill.data_dir_audit_runner import (
    _CI_MONITOR_STATE_CAP_BYTES,
    _CI_PATTERNS_CAP_BYTES,
    _GENERIC_JSON_CAP_BYTES,
    _RUNS_JSON_CAP_BYTES,
    _RUNS_JSON_MAX_ENTRIES,
    DataDirAuditPassResult,
    OrphanWorkspace,
    _compute_growth_deltas,
    _enumerate_boards,
    _growth_state_path,
    _load_growth_state,
    _save_growth_state,
    _scan_board_sizes,
    check_unbounded_candidates,
    find_largest_items,
    find_orphan_workspaces,
    run_data_dir_audit_pass,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(tmp_path: Path, **overrides) -> Settings:
    """Build a fresh Settings rooted at *tmp_path*.

    Resets engine + secrets + repos config so each test gets a clean
    per-board DB cache (the engine cache otherwise survives across
    tests). Extra keyword overrides are forwarded to ``Settings(...)``
    so the unbounded-candidate tests can dial ``max_memory_chars``
    down.
    """
    from robotsix_mill.config import _reset_repos_config

    _reset_secrets()
    db.reset_engine()
    _reset_repos_config()
    overrides.setdefault("data_dir", str(tmp_path))
    overrides.setdefault("require_approval", "false")
    return Settings(**overrides)


def _test_repo_config():
    """Synthetic RepoConfig for periodic-runner tests."""
    from robotsix_mill.config import RepoConfig

    return RepoConfig(
        repo_id="test-repo",
        board_id="test-board",
        langfuse_project_name="test-project",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
    )


def _make_workspace_dir(settings: Settings, board_id: str, ticket_id: str):
    """Create ``<data_dir>/<board>/workspaces/<ticket_id>/`` with a
    tiny payload file so dir-size accounting has something to sum."""
    ws_dir = settings.workspaces_dir_for(board_id) / ticket_id
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "description.md").write_text("hello\n")
    return ws_dir


def _insert_ticket(settings: Settings, board_id: str, ticket_id: str) -> None:
    """Insert a minimal Ticket row so the orphan scan sees it."""
    db.init_db(settings, board_id)
    with db.session(settings, board_id) as s:
        s.add(
            Ticket(
                id=ticket_id,
                title="t",
                state=State.DRAFT,
                workspace_path=str(settings.workspaces_dir_for(board_id) / ticket_id),
                board_id=board_id,
            )
        )
        s.commit()


def _write_bytes(path: Path, size: int, *, fill: bytes = b"x") -> None:
    """Create ``path`` containing exactly ``size`` bytes of ``fill``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(fill * size)


# ---------------------------------------------------------------------------
# ticket 2 — Unit tests for find_largest_items
# ---------------------------------------------------------------------------


def test_no_data_dir(tmp_path):
    """Returns [] when data_dir doesn't exist."""
    data_dir = tmp_path / "nope"
    result = find_largest_items(data_dir)
    assert result == []


def test_empty_data_dir(tmp_path):
    """Returns [] when data_dir exists but is empty."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    result = find_largest_items(data_dir)
    assert result == []


def test_all_below_threshold(tmp_path):
    """Returns [] when all items are below threshold."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "small.txt").write_bytes(b"x" * 50_000_000)  # 50 MB
    result = find_largest_items(data_dir, threshold_bytes=100 * 1024 * 1024)
    assert result == []


def test_single_oversized_file(tmp_path):
    """A single 200 MB file is reported correctly."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    fp = data_dir / "big.bin"
    fd = os.open(str(fp), os.O_CREAT | os.O_WRONLY)
    try:
        os.ftruncate(fd, 200 * 1024 * 1024)
    finally:
        os.close(fd)

    result = find_largest_items(data_dir, threshold_bytes=100 * 1024 * 1024)
    assert len(result) == 1
    item = result[0]
    assert item["path"] == "big.bin"
    assert item["size_bytes"] == 200 * 1024 * 1024
    assert item["is_directory"] is False


def test_oversized_directory(tmp_path):
    """A directory with 3×50 MB files is reported with cumulative size."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    sub = data_dir / "subdir"
    sub.mkdir()
    for i in range(3):
        fp = sub / f"f{i}.bin"
        fd = os.open(str(fp), os.O_CREAT | os.O_WRONLY)
        try:
            os.ftruncate(fd, 50 * 1024 * 1024)
        finally:
            os.close(fd)

    result = find_largest_items(data_dir, threshold_bytes=100 * 1024 * 1024)
    assert len(result) == 1
    item = result[0]
    assert item["path"] == "subdir"
    assert item["size_bytes"] == 150 * 1024 * 1024
    assert item["is_directory"] is True


def test_top_n_limit(tmp_path):
    """Only top_n (10) items are returned when many oversized items exist."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    sizes_bytes = []
    for i in range(15):
        fp = data_dir / f"big_{i:02d}.bin"
        size = (15 - i) * 50 * 1024 * 1024  # descending actual sizes
        fd = os.open(str(fp), os.O_CREAT | os.O_WRONLY)
        try:
            os.ftruncate(fd, size)
        finally:
            os.close(fd)
        sizes_bytes.append(size)

    result = find_largest_items(data_dir, top_n=10, threshold_bytes=0)
    assert len(result) == 10
    # Verify descending by size
    for prev, cur in zip(result, result[1:]):
        assert prev["size_bytes"] >= cur["size_bytes"]


def test_top_n_tie_breaking(tmp_path):
    """Equal-size items are tie-broken by path lexicographically."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # Create two files with same size
    (data_dir / "b_file.bin").write_bytes(b"x" * 100)
    (data_dir / "a_file.bin").write_bytes(b"x" * 100)

    result = find_largest_items(data_dir, top_n=10, threshold_bytes=0)
    # Filter to just the two tie entries
    paths = [r["path"] for r in result if r["path"] in ("a_file.bin", "b_file.bin")]
    assert paths == ["a_file.bin", "b_file.bin"]


def test_symlink_skipped(tmp_path):
    """Symlinks are skipped and do not appear in results."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    target = data_dir / "real_big.bin"
    fd = os.open(str(target), os.O_CREAT | os.O_WRONLY)
    try:
        os.ftruncate(fd, 200 * 1024 * 1024)
    finally:
        os.close(fd)

    link = data_dir / "link_to_big.bin"
    os.symlink(str(target), str(link))

    result = find_largest_items(data_dir, threshold_bytes=100 * 1024 * 1024)
    paths = [r["path"] for r in result]
    assert "link_to_big.bin" not in paths
    # The target may or may not be walked — with followlinks=False,
    # os.walk through the symlink won't include the target, but the
    # target is still inside data_dir and will be found by normal
    # directory traversal
    if "real_big.bin" in paths:
        # it's fine if the real file is found via normal walk
        pass
    else:
        # also fine — the key is the symlink is skipped
        pass


def test_permission_error_graceful(tmp_path, caplog, monkeypatch):
    """Permission errors log a warning and don't crash the pass."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # Create a readable oversized file
    (data_dir / "good.bin").write_bytes(b"x" * (200 * 1024 * 1024))
    # Create a file that will trigger OSError from getsize
    (data_dir / "bad.bin").write_bytes(b"x" * 100_000_000)

    # Monkeypatch os.path.getsize to raise OSError for the bad file.
    # (chmod 0o000 does NOT block stat on Linux for the file owner, so
    # we simulate the fault path directly.)
    original_getsize = os.path.getsize

    def mock_getsize(path):
        if path.endswith("bad.bin"):
            raise OSError("Permission denied")
        return original_getsize(path)

    monkeypatch.setattr(os.path, "getsize", mock_getsize)

    with caplog.at_level(logging.WARNING, logger="robotsix_mill.data_dir_audit"):
        result = find_largest_items(data_dir, threshold_bytes=100 * 1024 * 1024)

    # The good file should still be reported
    assert len(result) >= 1
    paths = [r["path"] for r in result]
    assert "good.bin" in paths
    assert "bad.bin" not in paths
    # A warning should have been logged
    assert any("Cannot access" in rec.message for rec in caplog.records)


def test_threshold_zero(tmp_path):
    """threshold_bytes=0 returns every non-empty file (up to top_n)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for i in range(5):
        (data_dir / f"f{i}.txt").write_bytes(b"x" * (i + 1))

    result = find_largest_items(data_dir, top_n=10, threshold_bytes=0)
    assert len(result) == 5  # all non-empty files
    paths = {r["path"] for r in result}
    for i in range(5):
        assert f"f{i}.txt" in paths


def test_mixed_files_and_dirs(tmp_path):
    """Both oversized files and oversized directories appear together."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # Oversized file
    fd = os.open(str(data_dir / "big_file.bin"), os.O_CREAT | os.O_WRONLY)
    try:
        os.ftruncate(fd, 200 * 1024 * 1024)
    finally:
        os.close(fd)
    # Oversized directory
    sub = data_dir / "big_dir"
    sub.mkdir()
    for i in range(3):
        fp = sub / f"f{i}.bin"
        fd2 = os.open(str(fp), os.O_CREAT | os.O_WRONLY)
        try:
            os.ftruncate(fd2, 50 * 1024 * 1024)
        finally:
            os.close(fd2)

    result = find_largest_items(data_dir, threshold_bytes=100 * 1024 * 1024)
    paths = {r["path"] for r in result}
    assert "big_file.bin" in paths
    assert "big_dir" in paths
    # Sorted by size: file (200 MB) before dir (150 MB)
    assert result[0]["size_bytes"] >= result[1]["size_bytes"]


def test_root_data_dir_excluded(tmp_path):
    """The root data_dir itself is never in results."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # Put a huge file deep to make root huge
    sub = data_dir / "a" / "b" / "c"
    sub.mkdir(parents=True)
    fd = os.open(str(sub / "big.bin"), os.O_CREAT | os.O_WRONLY)
    try:
        os.ftruncate(fd, 200 * 1024 * 1024)
    finally:
        os.close(fd)

    result = find_largest_items(data_dir, threshold_bytes=100 * 1024 * 1024)
    paths = [r["path"] for r in result]
    assert "." not in paths
    assert "" not in paths
    # The subdirectories should appear (they inherited the size)
    for r in result:
        assert r["path"] != "."
        assert r["path"] != ""


# ---------------------------------------------------------------------------
# ticket 5 — find_orphan_workspaces
# ---------------------------------------------------------------------------


def test_no_orphans_when_no_workspaces_dir(tmp_path):
    """Missing ``workspaces/`` dir returns an empty list, no error."""
    s = _make_settings(tmp_path)
    db.init_db(s, "board-x")  # creates the mill.db but no workspaces/
    assert find_orphan_workspaces(s, "board-x") == []
    db.reset_engine()


def test_no_orphans_when_workspaces_dir_empty(tmp_path):
    """An existing-but-empty workspaces dir returns []."""
    s = _make_settings(tmp_path)
    db.init_db(s, "board-x")
    s.workspaces_dir_for("board-x").mkdir(parents=True)
    assert find_orphan_workspaces(s, "board-x") == []
    db.reset_engine()


def test_single_orphan_detected(tmp_path):
    """A workspace dir with no matching Ticket row is reported as orphan."""
    s = _make_settings(tmp_path)
    db.init_db(s, "board-x")
    ticket_id = "20260101T000000Z-old-ticket-ab12"
    ws_dir = _make_workspace_dir(s, "board-x", ticket_id)

    orphans = find_orphan_workspaces(s, "board-x")

    assert len(orphans) == 1
    o = orphans[0]
    assert isinstance(o, OrphanWorkspace)
    assert o.board_id == "board-x"
    assert o.ticket_id == ticket_id
    assert o.path == ws_dir
    assert o.dir_size_bytes > 0
    db.reset_engine()


def test_active_ticket_not_flagged(tmp_path):
    """A workspace dir whose ticket exists in the DB is NOT reported."""
    s = _make_settings(tmp_path)
    ticket_id = "20260101T000000Z-active-ab12"
    _make_workspace_dir(s, "board-x", ticket_id)
    _insert_ticket(s, "board-x", ticket_id)

    assert find_orphan_workspaces(s, "board-x") == []
    db.reset_engine()


def test_non_ticket_id_dir_skipped_with_warning(tmp_path, caplog):
    """A non-ticket-shaped subdir is skipped + WARNING logged, never an orphan."""
    s = _make_settings(tmp_path)
    db.init_db(s, "board-x")
    ws_root = s.workspaces_dir_for("board-x")
    ws_root.mkdir(parents=True)
    (ws_root / "artifacts").mkdir()  # non-ticket-ID dir name
    (ws_root / ".gitkeep").write_text("")  # not a dir — also ignored

    with caplog.at_level(logging.WARNING, logger="robotsix_mill.data_dir_audit"):
        orphans = find_orphan_workspaces(s, "board-x")

    assert orphans == []
    assert any(
        "artifacts" in rec.message and "non-ticket-ID" in rec.message
        for rec in caplog.records
    )
    db.reset_engine()


def test_board_isolation(tmp_path):
    """A workspace dir in board A is matched against A's DB only."""
    s = _make_settings(tmp_path)
    ticket_id = "20260101T000000Z-shared-ab12"

    # Put the workspace dir under board-A; insert the matching ticket
    # ONLY under board-B. From board-A's perspective the ticket is
    # missing — so it must still be reported as orphan.
    _make_workspace_dir(s, "board-a", ticket_id)
    _insert_ticket(s, "board-b", ticket_id)
    # Make sure board-a's DB exists but does NOT contain the ticket.
    db.init_db(s, "board-a")

    orphans_a = find_orphan_workspaces(s, "board-a")
    assert len(orphans_a) == 1
    assert orphans_a[0].board_id == "board-a"
    assert orphans_a[0].ticket_id == ticket_id

    # And board-B has no workspace dir, so it reports no orphans.
    orphans_b = find_orphan_workspaces(s, "board-b")
    assert orphans_b == []
    db.reset_engine()


def test_batch_query_used_for_large_set(tmp_path, monkeypatch):
    """All candidate IDs are queried in batched ``IN`` selects, not
    one query per directory.

    We monkeypatch ``Session.exec`` to count invocations and assert
    the total is well below the candidate count (with default
    batch size 500, 600 candidates yields exactly 2 SELECT calls).
    """
    s = _make_settings(tmp_path)
    db.init_db(s, "board-x")

    # 600 orphan dirs — exceeds the 500 batch ceiling.
    for i in range(600):
        _make_workspace_dir(s, "board-x", f"20260101T000000Z-bulk-{i:04d}")

    from sqlmodel import Session

    calls: list[int] = []
    real_exec = Session.exec

    def counting_exec(self, *a, **k):
        calls.append(1)
        return real_exec(self, *a, **k)

    monkeypatch.setattr(Session, "exec", counting_exec)

    orphans = find_orphan_workspaces(s, "board-x")

    assert len(orphans) == 600
    # 600 candidates / 500 batch size = 2 SELECT calls. Anything
    # close to one-query-per-directory would be 600+.
    assert len(calls) == 2
    db.reset_engine()


# ---------------------------------------------------------------------------
# ticket 5 — run_data_dir_audit_pass orphan integration
# ---------------------------------------------------------------------------


def test_pass_reports_orphans_per_board_in_summary(tmp_path, monkeypatch):
    """``run_data_dir_audit_pass`` discovers boards from disk, scans
    each, and includes orphan counts (with per-board detail) in the
    summary string."""
    s = _make_settings(tmp_path)

    # Two boards, each with one orphan workspace.
    _make_workspace_dir(s, "board-a", "20260101T000000Z-orph-aa11")
    _make_workspace_dir(s, "board-b", "20260101T000000Z-orph-bb22")
    db.init_db(s, "board-a")
    db.init_db(s, "board-b")

    monkeypatch.setattr(
        "robotsix_mill.data_dir_audit_runner.Settings",
        lambda: s,
    )

    result = run_data_dir_audit_pass(session_id="sess-1")

    assert result.session_id == "sess-1"
    assert result.drafts_created == []
    assert "orphan workspaces" in result.summary
    assert "2" in result.summary  # total
    assert "board-a=1" in result.summary
    assert "board-b=1" in result.summary
    db.reset_engine()


def test_pass_no_orphans_when_clean(tmp_path, monkeypatch):
    """With no orphans the orphan section is omitted from the summary.

    The growth-delta status is still emitted (ticket 3 integration);
    oversized-items and unbounded-collection sections only appear
    when something is flagged.
    """
    s = _make_settings(tmp_path)
    db.init_db(s, "board-clean")

    monkeypatch.setattr(
        "robotsix_mill.data_dir_audit_runner.Settings",
        lambda: s,
    )

    result = run_data_dir_audit_pass(session_id="sess-clean")
    assert "orphan" not in result.summary
    assert result.drafts_created == []
    db.reset_engine()


def test_pass_integrates_find_largest_items(tmp_path, monkeypatch):
    """run_data_dir_audit_pass wires find_largest_items and populates
    oversized_items."""
    settings = _make_settings(tmp_path)
    # Place an oversized file in data_dir
    fd = os.open(str(settings.data_dir / "huge.bin"), os.O_CREAT | os.O_WRONLY)
    try:
        os.ftruncate(fd, 200 * 1024 * 1024)
    finally:
        os.close(fd)

    monkeypatch.setattr(
        "robotsix_mill.data_dir_audit_runner.Settings", lambda: settings
    )

    result = run_data_dir_audit_pass(
        session_id="test-sid", repo_config=_test_repo_config()
    )

    assert isinstance(result, DataDirAuditPassResult)
    assert result.drafts_created == []
    assert result.session_id == "test-sid"
    assert len(result.oversized_items) == 1
    assert result.oversized_items[0]["path"] == "huge.bin"
    assert result.oversized_items[0]["size_bytes"] == 200 * 1024 * 1024
    assert result.oversized_items[0]["is_directory"] is False
    assert "1 oversized items" in result.summary


# ---------------------------------------------------------------------------
# ticket 4 — check_unbounded_candidates: specific patterns
# ---------------------------------------------------------------------------


class TestMemoryLedger:
    def test_memory_md_over_cap_flagged(self, tmp_path):
        settings = _make_settings(tmp_path, max_memory_chars=100)
        ledger = tmp_path / "implement_memory.md"
        _write_bytes(ledger, 200)

        findings = check_unbounded_candidates(tmp_path, settings)

        assert len(findings) == 1
        f = findings[0]
        assert f["pattern"] == "*_memory.md"
        assert f["path"] == "implement_memory.md"
        assert f["current_size"] == 200
        assert f["cap_size"] == 100
        assert f["cap_detail"] == "max_memory_chars=100"
        assert f["check"] == "unbounded_candidates"
        assert f["severity"] == "warning"
        assert f["record_count"] is None
        assert f["record_max"] is None

    def test_memory_md_under_cap_not_flagged(self, tmp_path):
        settings = _make_settings(tmp_path, max_memory_chars=8000)
        ledger = tmp_path / "implement_memory.md"
        _write_bytes(ledger, 100)

        findings = check_unbounded_candidates(tmp_path, settings)

        assert findings == []

    def test_nested_memory_md_flagged(self, tmp_path):
        """Memory ledgers under per-board subdirectories are picked up
        by the recursive ``rglob``."""
        settings = _make_settings(tmp_path, max_memory_chars=100)
        ledger = tmp_path / "board-a" / "refine_memory.md"
        _write_bytes(ledger, 500)

        findings = check_unbounded_candidates(tmp_path, settings)

        assert len(findings) == 1
        assert findings[0]["path"] == str(Path("board-a") / "refine_memory.md")
        assert findings[0]["pattern"] == "*_memory.md"


class TestRunsJson:
    def test_runs_json_over_size_cap_flagged(self, tmp_path):
        settings = _make_settings(tmp_path)
        runs = tmp_path / "runs.json"
        runs.write_text("[]" + " " * (_RUNS_JSON_CAP_BYTES))  # > 25 KB

        findings = check_unbounded_candidates(tmp_path, settings)

        assert len(findings) == 1
        f = findings[0]
        assert f["pattern"] == "runs.json"
        assert f["path"] == "runs.json"
        assert f["cap_size"] == _RUNS_JSON_CAP_BYTES
        assert f["current_size"] > _RUNS_JSON_CAP_BYTES
        assert f["cap_detail"] == f"MAX_ENTRIES={_RUNS_JSON_MAX_ENTRIES} (~25 KB)"
        # Size-only flag: record_count should be None when JSON
        # parses but entries are <= MAX_ENTRIES.
        assert f["record_count"] is None
        assert f["record_max"] is None

    def test_runs_json_over_entry_count_flagged(self, tmp_path):
        settings = _make_settings(tmp_path)
        runs = tmp_path / "runs.json"
        # 60 small entries — under 25 KB but over MAX_ENTRIES=50.
        entries = [{"id": i} for i in range(60)]
        runs.write_text(json.dumps(entries))

        findings = check_unbounded_candidates(tmp_path, settings)

        assert len(findings) == 1
        f = findings[0]
        assert f["pattern"] == "runs.json"
        assert f["record_count"] == 60
        assert f["record_max"] == _RUNS_JSON_MAX_ENTRIES
        # Size is under the cap — the flag is triggered solely by the
        # record count.
        assert f["current_size"] < _RUNS_JSON_CAP_BYTES

    def test_runs_json_under_both_caps_not_flagged(self, tmp_path):
        settings = _make_settings(tmp_path)
        runs = tmp_path / "runs.json"
        runs.write_text(json.dumps([{"id": i} for i in range(10)]))

        findings = check_unbounded_candidates(tmp_path, settings)

        assert findings == []

    def test_runs_json_corrupt_does_not_crash(self, tmp_path, caplog):
        """A malformed runs.json must not raise — it is silently skipped
        with a debug-level log entry. Size check still applies."""
        settings = _make_settings(tmp_path)
        runs = tmp_path / "runs.json"
        runs.write_text("not valid json {{{")

        with caplog.at_level(logging.DEBUG, logger="robotsix_mill.data_dir_audit"):
            findings = check_unbounded_candidates(tmp_path, settings)

        # File is small and JSON is corrupt → no flag, no crash.
        assert findings == []
        # And a debug log was emitted noting the parse skip.
        assert any(
            "runs.json" in rec.message and "record-count check" in rec.message
            for rec in caplog.records
        )

    def test_runs_json_corrupt_but_oversized_still_flagged_by_size(self, tmp_path):
        """Corrupt JSON does NOT silence the size check — a runs.json
        whose bytes exceed the 25 KB cap is still flagged on size."""
        settings = _make_settings(tmp_path)
        runs = tmp_path / "runs.json"
        runs.write_text("not valid json {{{" + " " * _RUNS_JSON_CAP_BYTES)

        findings = check_unbounded_candidates(tmp_path, settings)

        assert len(findings) == 1
        assert findings[0]["pattern"] == "runs.json"
        # No record-count info — JSON parse failed.
        assert findings[0]["record_count"] is None
        assert findings[0]["record_max"] is None


class TestCiPatternsJson:
    def test_ci_patterns_over_cap_flagged(self, tmp_path):
        settings = _make_settings(tmp_path)
        path = tmp_path / "ci_patterns.json"
        _write_bytes(path, _CI_PATTERNS_CAP_BYTES + 1)

        findings = check_unbounded_candidates(tmp_path, settings)

        assert len(findings) == 1
        f = findings[0]
        assert f["pattern"] == "ci_patterns.json"
        assert f["cap_size"] == _CI_PATTERNS_CAP_BYTES
        assert f["cap_detail"] == "default=1 MB"

    def test_ci_patterns_under_cap_not_flagged(self, tmp_path):
        settings = _make_settings(tmp_path)
        path = tmp_path / "ci_patterns.json"
        _write_bytes(path, 1024)

        findings = check_unbounded_candidates(tmp_path, settings)

        assert findings == []


class TestCiMonitorState:
    def test_ci_monitor_state_over_cap_flagged(self, tmp_path):
        settings = _make_settings(tmp_path)
        path = tmp_path / "ci_monitor_state.json"
        _write_bytes(path, _CI_MONITOR_STATE_CAP_BYTES + 1)

        findings = check_unbounded_candidates(tmp_path, settings)

        assert len(findings) == 1
        f = findings[0]
        assert f["pattern"] == "ci_monitor_state.json"
        assert f["cap_size"] == _CI_MONITOR_STATE_CAP_BYTES
        assert f["cap_detail"] == "default=500 KB"

    def test_ci_monitor_state_under_cap_not_flagged(self, tmp_path):
        settings = _make_settings(tmp_path)
        path = tmp_path / "ci_monitor_state.json"
        _write_bytes(path, 1024)

        findings = check_unbounded_candidates(tmp_path, settings)

        assert findings == []


class TestGenericJson:
    def test_generic_json_over_cap_flagged(self, tmp_path):
        settings = _make_settings(tmp_path)
        path = tmp_path / "subdir" / "some_registry.json"
        _write_bytes(path, _GENERIC_JSON_CAP_BYTES + 1)

        findings = check_unbounded_candidates(tmp_path, settings)

        assert len(findings) == 1
        f = findings[0]
        assert f["pattern"] == "*.json"
        assert f["cap_size"] == _GENERIC_JSON_CAP_BYTES
        assert f["cap_detail"] == "default=5 MB"

    def test_generic_json_under_cap_not_flagged(self, tmp_path):
        settings = _make_settings(tmp_path)
        path = tmp_path / "some_registry.json"
        _write_bytes(path, 1024)

        findings = check_unbounded_candidates(tmp_path, settings)

        assert findings == []

    def test_specific_pattern_excluded_from_generic(self, tmp_path):
        """A ``runs.json`` matched by the specific pattern must NOT
        also be flagged by the generic ``*.json`` pattern (no
        double-flagging)."""
        settings = _make_settings(tmp_path)
        runs = tmp_path / "runs.json"
        # Over both caps — the spec says only the specific pattern
        # claims the file.
        _write_bytes(runs, _GENERIC_JSON_CAP_BYTES + 1)

        findings = check_unbounded_candidates(tmp_path, settings)

        assert len(findings) == 1
        assert findings[0]["pattern"] == "runs.json"


class TestNonMatchingFiles:
    def test_non_matching_file_ignored(self, tmp_path):
        """Files that don't match any pattern are not checked, no
        matter how large they are."""
        settings = _make_settings(tmp_path)
        path = tmp_path / "huge_blob.bin"
        _write_bytes(path, _GENERIC_JSON_CAP_BYTES + 1)

        findings = check_unbounded_candidates(tmp_path, settings)

        assert findings == []

    def test_missing_data_dir_returns_empty(self, tmp_path):
        """A nonexistent ``data_dir`` returns an empty list rather
        than raising."""
        settings = _make_settings(tmp_path)
        missing = tmp_path / "does_not_exist"

        findings = check_unbounded_candidates(missing, settings)

        assert findings == []


# ---------------------------------------------------------------------------
# ticket 4 — Integration with run_data_dir_audit_pass
# ---------------------------------------------------------------------------


class TestRunDataDirAuditPass:
    def test_returns_findings_and_summary(self, tmp_path, monkeypatch):
        """The pass result must surface flagged findings AND reflect
        the count in ``summary``."""
        settings = _make_settings(tmp_path, max_memory_chars=100)
        ledger = tmp_path / "implement_memory.md"
        _write_bytes(ledger, 500)

        # Tests inject Settings via the module-level monkeypatch seam.
        monkeypatch.setattr(
            "robotsix_mill.data_dir_audit_runner.Settings",
            lambda: settings,
        )

        result = run_data_dir_audit_pass()

        assert isinstance(result, DataDirAuditPassResult)
        assert len(result.findings) == 1
        assert result.findings[0]["pattern"] == "*_memory.md"
        # Summary always includes growth-delta status (unconditional);
        # the unbounded segment is appended when findings exist.
        assert "1 unbounded-collection candidate(s) flagged" in result.summary

    def test_no_findings_summary(self, tmp_path, monkeypatch):
        settings = _make_settings(tmp_path)
        monkeypatch.setattr(
            "robotsix_mill.data_dir_audit_runner.Settings",
            lambda: settings,
        )

        result = run_data_dir_audit_pass()

        assert result.findings == []
        # Unbounded section omitted (no findings); growth-delta status
        # is always present and shows the no-threshold-exceeded message
        # with no boards on disk.
        assert "unbounded-collection" not in result.summary

    def test_findings_field_defaults_to_empty(self):
        """``DataDirAuditPassResult.findings`` defaults to ``[]``."""
        r = DataDirAuditPassResult(drafts_created=[], summary="no findings")
        assert r.findings == []


# ---------------------------------------------------------------------------
# Session/repo passthrough
# ---------------------------------------------------------------------------


def test_run_pass_propagates_session_id(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.data_dir_audit_runner.Settings",
        lambda: settings,
    )

    result = run_data_dir_audit_pass(session_id="abc-123")

    assert result.session_id == "abc-123"


# ---------------------------------------------------------------------------
# State path (ticket 3)
# ---------------------------------------------------------------------------


def test_growth_state_path_format(tmp_path):
    s = _make_settings(tmp_path)
    path = _growth_state_path(s, "my-board")
    assert path == s.data_dir / "my-board" / "data_dir_audit_state.json"
    assert path.name == "data_dir_audit_state.json"


# ---------------------------------------------------------------------------
# State load
# ---------------------------------------------------------------------------


def test_load_growth_state_file_absent(tmp_path, caplog):
    path = tmp_path / "no_such_file.json"
    result = _load_growth_state(path)
    assert result == {}
    assert not caplog.text  # no warning for first-run


def test_load_growth_state_valid_json(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"a/b.txt": {"size_bytes": 100, "mtime": 1.0}}), encoding="utf-8"
    )
    result = _load_growth_state(path)
    assert result == {"a/b.txt": {"size_bytes": 100, "mtime": 1.0}}


def test_load_growth_state_corrupt_json(tmp_path, caplog):
    path = tmp_path / "corrupt.json"
    path.write_text("this is not json", encoding="utf-8")
    result = _load_growth_state(path)
    assert result == {}
    assert "unreadable" in caplog.text.lower()


def test_load_growth_state_filters_non_dict_values(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "good": {"size_bytes": 10, "mtime": 1.0},
                "bad_not_dict": "string_value",
                "bad_missing_size": {"mtime": 2.0},
                "bad_missing_mtime": {"size_bytes": 3},
            }
        ),
        encoding="utf-8",
    )
    result = _load_growth_state(path)
    assert set(result.keys()) == {"good"}
    assert result["good"] == {"size_bytes": 10, "mtime": 1.0}


# ---------------------------------------------------------------------------
# State save
# ---------------------------------------------------------------------------


def test_save_growth_state_atomic_write(tmp_path):
    path = tmp_path / "subdir" / "state.json"
    state = {"file.txt": {"size_bytes": 500, "mtime": 99.0}}
    _save_growth_state(path, state)

    # Main file exists and is valid JSON
    assert path.exists()
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded == state

    # Pretty-printed
    raw = path.read_text(encoding="utf-8")
    assert "  " in raw  # indent=2

    # .tmp file should not exist after successful write
    tmp_path_actual = path.with_suffix(".json.tmp")
    assert not tmp_path_actual.exists()


def test_save_growth_state_overwrites_previous(tmp_path):
    path = tmp_path / "state.json"
    old = {"old.txt": {"size_bytes": 1, "mtime": 1.0}}
    new = {"new.txt": {"size_bytes": 999, "mtime": 2.0}}
    _save_growth_state(path, old)
    _save_growth_state(path, new)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded == new


# ---------------------------------------------------------------------------
# Board enumeration
# ---------------------------------------------------------------------------


def test_enumerate_boards_no_boards(tmp_path):
    s = _make_settings(tmp_path)
    # No mill.db anywhere
    (s.data_dir / "empty").mkdir(parents=True)
    assert _enumerate_boards(s) == []


def test_enumerate_boards_with_board(tmp_path):
    s = _make_settings(tmp_path)
    board = s.data_dir / "my-board"
    board.mkdir(parents=True)
    (board / "mill.db").touch()
    (board / "workspaces").mkdir()
    assert _enumerate_boards(s) == ["my-board"]


def test_enumerate_boards_skips_non_dirs(tmp_path):
    s = _make_settings(tmp_path)
    (s.data_dir / "not-a-dir").touch()  # regular file
    board = s.data_dir / "real-board"
    board.mkdir(parents=True)
    (board / "mill.db").touch()
    assert _enumerate_boards(s) == ["real-board"]


def test_enumerate_boards_missing_mill_db(tmp_path):
    s = _make_settings(tmp_path)
    (s.data_dir / "no-db").mkdir(parents=True)
    assert _enumerate_boards(s) == []


# ---------------------------------------------------------------------------
# Size scan
# ---------------------------------------------------------------------------


def test_scan_board_sizes_files_and_dirs(tmp_path):
    board = tmp_path / "board"
    board.mkdir()
    (board / "a.txt").write_bytes(b"hello")  # 5 bytes
    (board / "sub").mkdir()
    (board / "sub" / "b.txt").write_bytes(b"world!")  # 6 bytes

    sizes = _scan_board_sizes(board)

    # Files
    assert sizes["a.txt"]["size_bytes"] == 5
    assert sizes["sub/b.txt"]["size_bytes"] == 6

    # Cumulative directory sizes — no double-counting
    assert sizes["sub/"]["size_bytes"] == 6
    assert sizes["sub/"]["size_bytes"] == 6  # exactly the file under it

    # Parent directory = a.txt + sub contents = 5 + 6 = 11
    # (root dir key is "./")
    # Note: root dir may or may not be present depending on walk
    # Let's just verify non-root dirs
    assert sizes["sub/"]["size_bytes"] == 6


def test_scan_board_sizes_deep_nesting_no_double_count(tmp_path):
    """Verify the double-counting bug is fixed: parent directories sum
    each file exactly once."""
    board = tmp_path / "board"
    board.mkdir()
    (board / "a").mkdir()
    (board / "a" / "sub").mkdir()
    (board / "a" / "sub" / "deep").mkdir()
    (board / "a" / "file1.txt").write_bytes(b"x" * 100)
    (board / "a" / "sub" / "file2.txt").write_bytes(b"y" * 200)
    (board / "a" / "sub" / "deep" / "file3.txt").write_bytes(b"z" * 300)

    sizes = _scan_board_sizes(board)

    assert sizes["a/sub/deep/"]["size_bytes"] == 300
    assert sizes["a/sub/"]["size_bytes"] == 500  # 200 + 300
    assert sizes["a/"]["size_bytes"] == 600  # 100 + 200 + 300


def test_scan_board_sizes_skips_symlinks(tmp_path):
    board = tmp_path / "board"
    board.mkdir()
    (board / "real.txt").write_bytes(b"abc")
    # Create a symlink (may fail on some platforms — skip gracefully)
    try:
        (board / "link.txt").symlink_to(board / "real.txt")
    except OSError:
        pytest.skip("symlink not supported on this platform")

    sizes = _scan_board_sizes(board)
    assert "real.txt" in sizes
    # symlink should be excluded
    link_keys = [k for k in sizes if "link" in k]
    assert link_keys == []


def test_scan_board_sizes_excludes_state_file(tmp_path):
    board = tmp_path / "board"
    board.mkdir()
    (board / "data_dir_audit_state.json").write_text("{}")
    (board / "other.txt").write_bytes(b"stuff")

    sizes = _scan_board_sizes(board)
    assert "data_dir_audit_state.json" not in sizes
    assert "other.txt" in sizes


def test_scan_board_sizes_empty_board(tmp_path):
    board = tmp_path / "board"
    board.mkdir()
    sizes = _scan_board_sizes(board)
    # May have root dir entry with size 0
    for info in sizes.values():
        if isinstance(info, dict) and "size_bytes" in info:
            assert info["size_bytes"] == 0


def test_scan_board_sizes_mtime_present(tmp_path):
    board = tmp_path / "board"
    board.mkdir()
    (board / "f.txt").write_bytes(b"data")
    sizes = _scan_board_sizes(board)
    assert "mtime" in sizes["f.txt"]
    assert isinstance(sizes["f.txt"]["mtime"], float)
    assert sizes["f.txt"]["mtime"] > 0


# ---------------------------------------------------------------------------
# Delta computation
# ---------------------------------------------------------------------------


def _s(settings=None, **overrides):
    """Shorthand to make a tiny Settings with defaults suitable for delta tests."""
    if settings is None:
        import copy

        s = Settings(data_dir="/tmp")
        s = copy.deepcopy(s)
    else:
        s = settings
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


class TestDeltaCompute:
    def test_no_prior(self):
        flags = _compute_growth_deltas({}, {"f": {"size_bytes": 10, "mtime": 1}}, _s())
        assert flags == []

    def test_shrink_no_flag(self):
        prior = {"f": {"size_bytes": 100, "mtime": 1}}
        current = {"f": {"size_bytes": 50, "mtime": 2}}
        flags = _compute_growth_deltas(prior, current, _s())
        assert flags == []

    def test_unchanged_no_flag(self):
        prior = {"f": {"size_bytes": 100, "mtime": 1}}
        current = {"f": {"size_bytes": 100, "mtime": 2}}
        flags = _compute_growth_deltas(prior, current, _s())
        assert flags == []

    def test_bytes_threshold_exceeded(self):
        prior = {"f": {"size_bytes": 1_000_000, "mtime": 1}}
        current = {"f": {"size_bytes": 12_000_000, "mtime": 2}}
        flags = _compute_growth_deltas(
            prior,
            current,
            _s(
                data_dir_audit_growth_delta_bytes=10_000_000,
                data_dir_audit_growth_delta_pct=9999,
            ),
        )
        assert len(flags) == 1
        assert flags[0]["threshold_exceeded"] == "bytes"
        assert flags[0]["delta_bytes"] == 11_000_000

    def test_pct_threshold_exceeded(self):
        prior = {"f": {"size_bytes": 10_000_000, "mtime": 1}}
        current = {"f": {"size_bytes": 14_000_000, "mtime": 2}}
        flags = _compute_growth_deltas(
            prior,
            current,
            _s(
                data_dir_audit_growth_delta_bytes=10_000_000,
                data_dir_audit_growth_delta_pct=20,
            ),
        )
        assert len(flags) == 1
        # 4M growth < 10M bytes threshold, but 40% >= 20%
        assert flags[0]["threshold_exceeded"] == "pct"
        assert flags[0]["delta_pct"] == 40.0

    def test_both_thresholds(self):
        prior = {"f": {"size_bytes": 1_000_000, "mtime": 1}}
        current = {"f": {"size_bytes": 20_000_000, "mtime": 2}}
        flags = _compute_growth_deltas(
            prior,
            current,
            _s(
                data_dir_audit_growth_delta_bytes=10_000_000,
                data_dir_audit_growth_delta_pct=20,
            ),
        )
        assert len(flags) == 1
        assert flags[0]["threshold_exceeded"] == "both"
        # 19M growth >= 10M, 1900% >= 20%

    def test_zero_prior_size_guard(self):
        prior = {"f": {"size_bytes": 0, "mtime": 1}}
        current = {"f": {"size_bytes": 500, "mtime": 2}}
        flags = _compute_growth_deltas(
            prior,
            current,
            _s(
                data_dir_audit_growth_delta_bytes=100,
                data_dir_audit_growth_delta_pct=20,
            ),
        )
        assert len(flags) == 1
        assert flags[0]["delta_pct"] == 100.0
        assert flags[0]["threshold_exceeded"] == "both"

    def test_deleted_path_not_compared(self):
        prior = {"f": {"size_bytes": 100, "mtime": 1}}
        current = {}  # f was deleted
        flags = _compute_growth_deltas(prior, current, _s())
        assert flags == []

    def test_directory_keys_compared(self):
        prior = {"d/": {"size_bytes": 1000, "mtime": 1}}
        current = {"d/": {"size_bytes": 1500, "mtime": 2}}
        flags = _compute_growth_deltas(
            prior,
            current,
            _s(
                data_dir_audit_growth_delta_bytes=100,
                data_dir_audit_growth_delta_pct=20,
            ),
        )
        assert len(flags) == 1
        assert flags[0]["path"] == "d/"
        assert flags[0]["threshold_exceeded"] == "both"


# ---------------------------------------------------------------------------
# Integration: run_data_dir_audit_pass — growth-delta
# ---------------------------------------------------------------------------


def _seed_board(board_dir: Path) -> None:
    board_dir.mkdir(parents=True, exist_ok=True)
    (board_dir / "mill.db").touch()
    (board_dir / "workspaces").mkdir(exist_ok=True)
    (board_dir / "big.log").write_bytes(b"x" * 100)


def test_first_run_no_prior_state(tmp_path, monkeypatch):
    """First run: no prior state → no growth flags, current scan saved."""
    s = _make_settings(tmp_path)
    monkeypatch.setattr("robotsix_mill.data_dir_audit_runner.Settings", lambda: s)

    board = s.data_dir / "test-board"
    _seed_board(board)

    result = run_data_dir_audit_pass()
    assert isinstance(result, DataDirAuditPassResult)
    assert result.growth_flags == []
    assert "no items exceeded thresholds" in result.summary

    # State file should exist now
    state_path = _growth_state_path(s, "test-board")
    assert state_path.exists()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "big.log" in state
    assert state["big.log"]["size_bytes"] == 100


def test_growth_detected_on_second_run(tmp_path, monkeypatch):
    """Second run picks up prior state and flags growth."""
    s = _make_settings(tmp_path)
    monkeypatch.setattr("robotsix_mill.data_dir_audit_runner.Settings", lambda: s)

    board = s.data_dir / "test-board"
    _seed_board(board)

    # First run — baseline
    run_data_dir_audit_pass()

    # Grow the file significantly
    (board / "big.log").write_bytes(b"x" * 20_000_000)  # ~19 MiB growth

    # Second run should detect growth
    result = run_data_dir_audit_pass()
    assert len(result.growth_flags) == 1
    flag = result.growth_flags[0]
    assert flag["check"] == "growth_delta"
    assert flag["path"] == "big.log"
    assert flag["threshold_exceeded"] in ("bytes", "both")
    assert "items flagged" in result.summary


def test_growth_ignores_shrink(tmp_path, monkeypatch):
    """Shrinking files should not be flagged."""
    s = _make_settings(tmp_path)
    monkeypatch.setattr("robotsix_mill.data_dir_audit_runner.Settings", lambda: s)

    board = s.data_dir / "test-board"
    _seed_board(board)
    (board / "big.log").write_bytes(b"x" * 10_000_000)

    run_data_dir_audit_pass()

    # Shrink the file
    (board / "big.log").write_bytes(b"x" * 1_000)

    result = run_data_dir_audit_pass()
    assert result.growth_flags == []
    assert "no items exceeded thresholds" in result.summary


def test_corrupt_state_recovered(tmp_path, monkeypatch, caplog):
    """Corrupt state file should be treated as first-run and overwritten."""
    s = _make_settings(tmp_path)
    monkeypatch.setattr("robotsix_mill.data_dir_audit_runner.Settings", lambda: s)

    board = s.data_dir / "test-board"
    _seed_board(board)

    # Write corrupt state
    state_path = _growth_state_path(s, "test-board")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("garbage not json", encoding="utf-8")

    result = run_data_dir_audit_pass()
    # Should have logged a warning about corrupt state
    assert "unreadable" in caplog.text.lower()
    # Should have overwritten the corrupt state with valid JSON
    assert state_path.exists()
    loaded = json.loads(state_path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    # First-run semantics: no prior → no flags
    assert result.growth_flags == []


def test_multiple_boards(tmp_path, monkeypatch):
    """Growth tracking works across multiple boards."""
    s = _make_settings(tmp_path)
    monkeypatch.setattr("robotsix_mill.data_dir_audit_runner.Settings", lambda: s)

    for bid in ("board-a", "board-b"):
        board = s.data_dir / bid
        _seed_board(board)

    # First run — baselines for both
    run_data_dir_audit_pass()

    # Grow file in board-a only
    (s.data_dir / "board-a" / "big.log").write_bytes(b"x" * 20_000_000)

    result = run_data_dir_audit_pass()
    flags = result.growth_flags
    assert len(flags) >= 1
    assert all(f["board_id"] == "board-a" for f in flags)
    assert "across 1 board" in result.summary


def test_result_summary_format(tmp_path, monkeypatch):
    """Verify the summary string format."""
    s = _make_settings(tmp_path)
    monkeypatch.setattr("robotsix_mill.data_dir_audit_runner.Settings", lambda: s)

    board = s.data_dir / "test-board"
    _seed_board(board)
    run_data_dir_audit_pass()
    (board / "big.log").write_bytes(b"x" * 30_000_000)

    result = run_data_dir_audit_pass()
    assert "growth-delta check:" in result.summary
    assert result.session_id == ""  # not set by default


# ---------------------------------------------------------------------------
# Shared engine cleanup
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _engine_cleanup():
    """Belt-and-braces: reset the engine cache before AND after each
    test, in case one of the asserts above raises before the inline
    ``reset_engine()`` runs."""
    db.reset_engine()
    yield
    db.reset_engine()
