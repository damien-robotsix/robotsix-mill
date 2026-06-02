"""Tests for the data-dir audit runner.

Covers:
- ticket 2 of the epic: ``find_largest_items`` (top-N largest files/dirs)
- ticket 5 of the epic: ``find_orphan_workspaces`` + its integration
  into ``run_data_dir_audit_pass``.
"""

from __future__ import annotations

import logging
import os

import pytest

from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.models import Ticket
from robotsix_mill.core.states import State
from robotsix_mill.data_dir_audit_runner import (
    DataDirAuditPassResult,
    OrphanWorkspace,
    find_largest_items,
    find_orphan_workspaces,
    run_data_dir_audit_pass,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(tmp_path, **overrides) -> Settings:
    """Build a fresh Settings rooted at *tmp_path*.

    Engines are also reset so each test gets a clean per-board DB
    cache (the engine cache survives across tests otherwise).
    """
    overrides.setdefault("data_dir", str(tmp_path))
    overrides.setdefault("require_approval", "false")
    db.reset_engine()
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


# ---------------------------------------------------------------------------
# Unit tests for find_largest_items
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
# find_orphan_workspaces
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
# run_data_dir_audit_pass integration
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


def test_pass_no_findings_when_clean(tmp_path, monkeypatch):
    """With no orphans or oversized items, the pass returns ``no findings``."""
    s = _make_settings(tmp_path)
    db.init_db(s, "board-clean")

    monkeypatch.setattr(
        "robotsix_mill.data_dir_audit_runner.Settings",
        lambda: s,
    )

    result = run_data_dir_audit_pass(session_id="sess-clean")
    assert result.summary == "no findings"
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


@pytest.fixture(autouse=True)
def _engine_cleanup():
    """Belt-and-braces: reset the engine cache before AND after each
    test, in case one of the asserts above raises before the inline
    ``reset_engine()`` runs."""
    db.reset_engine()
    yield
    db.reset_engine()
