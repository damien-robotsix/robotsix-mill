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
from datetime import timedelta
from pathlib import Path

import pytest

from robotsix_mill.config import Settings, _reset_secrets
from robotsix_mill.core import db
from robotsix_mill.core.models import SourceKind, Ticket, TicketEvent, _now
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.runners.data_dir_audit_runner import (
    _CI_MONITOR_STATE_CAP_BYTES,
    _CI_PATTERNS_CAP_BYTES,
    _GENERIC_JSON_CAP_BYTES,
    _RUNS_JSON_CAP_BYTES,
    _RUNS_JSON_MAX_ENTRIES,
    DataDirAuditPassResult,
    OrphanWorkspace,
    _compute_growth_deltas,
    _enumerate_boards,
    _file_findings_as_tickets,
    _growth_state_path,
    _is_meta_clone_cache_path,
    _is_periodic_pass_workspace_path,
    _load_growth_state,
    _prune_closed_workspaces,
    _save_growth_state,
    _scan_board_sizes,
    _workspace_ticket_id_for_path,
    check_unbounded_candidates,
    find_largest_items,
    find_orphan_workspaces,
    run_data_dir_audit_pass,
)
from robotsix_mill.runners.data_dir_audit.growth import (
    _GROWTH_CLASS_DB,
    _GROWTH_CLASS_OTHER,
    _classify_growth_path,
)
from robotsix_mill.runners.pass_runner import _GAP_ID_RE


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


def _make_sparse_file(path: Path, size: int) -> None:
    """Create a sparse file at *path* with apparent *size* bytes.

    Uses ``os.ftruncate`` so no actual blocks are written — safe for
    multi-hundred-MB test fixtures in constrained environments.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY)
    try:
        os.ftruncate(fd, size)
    finally:
        os.close(fd)


def _insert_closed_ticket(
    settings: Settings,
    board_id: str,
    ticket_id: str,
    *,
    closed_at,
    state: State = State.CLOSED,
) -> None:
    """Insert a terminal-state Ticket plus a backdated terminal
    ``TicketEvent`` (``at=closed_at``) so the prune age guard has a
    close time to measure against."""
    db.init_db(settings, board_id)
    with db.session(settings, board_id) as s:
        s.add(
            Ticket(
                id=ticket_id,
                title="t",
                state=state,
                workspace_path=str(settings.workspaces_dir_for(board_id) / ticket_id),
                board_id=board_id,
            )
        )
        s.add(
            TicketEvent(
                ticket_id=ticket_id,
                state=state,
                at=closed_at,
            )
        )
        s.commit()


def _insert_ticket_with_state(
    settings: Settings, board_id: str, ticket_id: str, state: State
) -> None:
    """Insert a Ticket row in an arbitrary (non-terminal) ``state``,
    with no terminal event."""
    db.init_db(settings, board_id)
    with db.session(settings, board_id) as s:
        s.add(
            Ticket(
                id=ticket_id,
                title="t",
                state=state,
                workspace_path=str(settings.workspaces_dir_for(board_id) / ticket_id),
                board_id=board_id,
            )
        )
        s.commit()


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
    for prev, cur in zip(result, result[1:], strict=False):
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
    summary string.  Orphan GC is disabled so old ticket-ID test
    fixtures survive into the scan."""
    s = _make_settings(tmp_path, data_dir_audit_prune_orphans=False)

    # Two boards, each with one orphan workspace.
    _make_workspace_dir(s, "board-a", "20260101T000000Z-orph-aa11")
    _make_workspace_dir(s, "board-b", "20260101T000000Z-orph-bb22")
    db.init_db(s, "board-a")
    db.init_db(s, "board-b")

    monkeypatch.setattr(
        "robotsix_mill.runners.data_dir_audit.Settings",
        lambda: s,
    )

    result = run_data_dir_audit_pass(session_id="sess-1")

    assert result.session_id == "sess-1"
    assert result.drafts_created == []
    assert "2 orphan workspaces" in result.summary
    assert result.summary.startswith("Scanned ")
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
        "robotsix_mill.runners.data_dir_audit.Settings",
        lambda: s,
    )

    result = run_data_dir_audit_pass(session_id="sess-clean")
    # No findings at all → header + zero-finding short-circuit. The
    # board's mill.db file is below the oversized threshold and the
    # other checks have nothing to flag.
    assert result.summary.startswith("Scanned ")
    assert "No issues found." in result.summary
    assert "\n" not in result.summary  # single line short-circuit
    assert result.drafts_created == []
    db.reset_engine()


# ---------------------------------------------------------------------------
# Orphan-workspace GC — prune old orphan dirs
# ---------------------------------------------------------------------------


def test_prune_orphan_removes_old_orphan_dir(tmp_path):
    """An orphan workspace dir older than the age threshold is deleted."""
    s = _make_settings(
        tmp_path,
        data_dir_audit_prune_orphans=True,
        data_dir_audit_prune_orphans_age_seconds=0,
    )
    ws = _make_workspace_dir(s, "board-x", "20200101T000000Z-old-orphan")
    db.init_db(s, "board-x")

    from robotsix_mill.runners.data_dir_audit.orphans import _prune_orphan_workspaces

    removed = _prune_orphan_workspaces(s)
    assert removed == 1
    assert not ws.exists()
    db.reset_engine()


def test_prune_orphan_keeps_recent_orphan_dir(tmp_path):
    """An orphan workspace dir younger than the age threshold is kept."""
    ticket_id = _now().strftime("%Y%m%dT%H%M%SZ") + "-recent-orphan"
    s = _make_settings(
        tmp_path,
        data_dir_audit_prune_orphans=True,
        data_dir_audit_prune_orphans_age_seconds=86_400,
    )
    ws = _make_workspace_dir(s, "board-x", ticket_id)
    db.init_db(s, "board-x")

    from robotsix_mill.runners.data_dir_audit.orphans import _prune_orphan_workspaces

    removed = _prune_orphan_workspaces(s)
    assert removed == 0
    assert ws.exists()
    db.reset_engine()


def test_prune_orphan_leaves_live_ticket_workspace(tmp_path):
    """A live ticket's workspace is never removed by the orphan GC."""
    s = _make_settings(
        tmp_path,
        data_dir_audit_prune_orphans=True,
        data_dir_audit_prune_orphans_age_seconds=0,
    )
    ticket_id = "20200101T000000Z-live-ticket"
    ws = _make_workspace_dir(s, "board-x", ticket_id)
    _insert_ticket(s, "board-x", ticket_id)

    from robotsix_mill.runners.data_dir_audit.orphans import _prune_orphan_workspaces

    removed = _prune_orphan_workspaces(s)
    assert removed == 0
    assert ws.exists()
    db.reset_engine()


def test_prune_orphan_knob_off_is_noop(tmp_path):
    """With the knob disabled, no orphan dirs are touched."""
    s = _make_settings(
        tmp_path,
        data_dir_audit_prune_orphans=False,
        data_dir_audit_prune_orphans_age_seconds=0,
    )
    ws = _make_workspace_dir(s, "board-x", "20200101T000000Z-old-orphan")
    db.init_db(s, "board-x")

    # Go through run_data_dir_audit_pass so the knob is checked.
    import robotsix_mill.runners.data_dir_audit as audit_mod

    original = audit_mod.Settings
    audit_mod.Settings = lambda: s
    try:
        result = run_data_dir_audit_pass()
    finally:
        audit_mod.Settings = original

    assert result.orphans_pruned == 0
    assert ws.exists()
    db.reset_engine()


def test_prune_orphan_summary_line(tmp_path, monkeypatch):
    """The summary includes 'Orphan workspaces pruned: N.' when orphans
    are GC'd."""
    s = _make_settings(
        tmp_path,
        data_dir_audit_prune_orphans=True,
        data_dir_audit_prune_orphans_age_seconds=0,
    )
    _make_workspace_dir(s, "board-x", "20200101T000000Z-old-orphan-1")
    _make_workspace_dir(s, "board-x", "20200101T000000Z-old-orphan-2")
    db.init_db(s, "board-x")

    monkeypatch.setattr(
        "robotsix_mill.runners.data_dir_audit.Settings",
        lambda: s,
    )

    result = run_data_dir_audit_pass()
    assert result.orphans_pruned == 2
    assert "Orphan workspaces pruned: 2." in result.summary
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
        "robotsix_mill.runners.data_dir_audit.Settings", lambda: settings
    )

    db.init_db(settings, "test-board")
    result = run_data_dir_audit_pass(
        session_id="test-sid", repo_config=_test_repo_config()
    )

    assert isinstance(result, DataDirAuditPassResult)
    assert result.session_id == "test-sid"
    assert len(result.oversized_items) == 1
    assert result.oversized_items[0]["path"] == "huge.bin"
    assert result.oversized_items[0]["size_bytes"] == 200 * 1024 * 1024
    assert result.oversized_items[0]["is_directory"] is False
    assert "1 oversized item " in result.summary
    # Filing logic (ticket 6) now creates one draft for the oversized
    # file when a board is available.
    assert len(result.drafts_created) == 1
    assert "oversized huge.bin" in result.drafts_created[0]["title"]


# ---------------------------------------------------------------------------
# ticket 4 — check_unbounded_candidates: specific patterns
# ---------------------------------------------------------------------------


class TestMemoryLedger:
    def test_memory_md_over_cap_flagged(self, tmp_path):
        settings = _make_settings(tmp_path, max_memory_chars=100)
        ledger = tmp_path / "implement_memory.md"
        # 500 bytes of 'x' = 500 chars, well above cap+tolerance (100+200=300).
        _write_bytes(ledger, 500)

        findings = check_unbounded_candidates(tmp_path, settings)

        assert len(findings) == 1
        f = findings[0]
        assert f["pattern"] == "*_memory.md"
        assert f["path"] == "implement_memory.md"
        assert f["current_size"] == 500
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

    def test_audit_state_file_excluded(self, tmp_path):
        """The audit's own ``data_dir_audit_state.json`` must NOT be
        flagged as an unbounded collection, even when it exceeds the
        generic ``*.json`` cap."""
        settings = _make_settings(tmp_path)
        path = tmp_path / "some_board" / "data_dir_audit_state.json"
        _write_bytes(path, _GENERIC_JSON_CAP_BYTES + 1)

        findings = check_unbounded_candidates(tmp_path, settings)

        assert findings == []


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


# ---------------------------------------------------------------------------
# ticket 4 — char-aware cap comparison + content embed
# ---------------------------------------------------------------------------


class TestCharAwareMemoryCap:
    """Tests for character-aware cap comparison for ``*_memory.md`` patterns
    (distinguishing char-capped from byte-capped), tolerance against
    transient post-write overages, deployed-data framing in the ticket
    body, and embedded file content."""

    def test_multibyte_at_cap_no_finding(self, tmp_path):
        """A ``*_memory.md`` whose character count is ≤ cap but whose
        UTF-8 byte size exceeds the cap (due to multibyte chars) is NOT
        flagged."""
        settings = _make_settings(tmp_path, max_memory_chars=100)
        ledger = tmp_path / "implement_memory.md"
        # 🔍 is 4 bytes in UTF-8, 1 char. 25 × 🔍 = 25 chars, 100 bytes.
        # + 75 single-byte chars = 100 chars total, 75 + 100 = 175 bytes > 100.
        text = ("🔍" * 25) + ("x" * 75)
        assert len(text) == 100
        assert len(text.encode("utf-8")) == 175  # bytes > cap (100)
        ledger.write_text(text, encoding="utf-8")

        findings = check_unbounded_candidates(tmp_path, settings)

        # Char count (100) ≤ cap (100) → NO finding, even though bytes > cap.
        assert findings == []

    def test_multibyte_just_under_cap_no_finding(self, tmp_path):
        """Chars ≤ cap but bytes well over cap (all multibyte) → no finding."""
        settings = _make_settings(tmp_path, max_memory_chars=100)
        ledger = tmp_path / "retrospect_memory.md"
        # 90 × 🔍 = 90 chars, 360 bytes. Under the char cap of 100.
        text = "🔍" * 90
        assert len(text) == 90
        assert len(text.encode("utf-8")) == 360
        ledger.write_text(text, encoding="utf-8")

        findings = check_unbounded_candidates(tmp_path, settings)
        assert findings == []

    def test_transient_overage_within_tolerance_no_finding(self, tmp_path):
        """A memory file only ~22 chars over the cap is within the
        ~2%/~200-char tolerance and is NOT flagged."""
        settings = _make_settings(tmp_path, max_memory_chars=8000)
        ledger = tmp_path / "retrospect_memory.md"
        # 8022 chars — 22 over the 8000 cap. Tolerance = max(160, 200) = 200.
        # 8022 ≤ 8200 → no finding.
        text = "x" * 8022
        assert len(text) == 8022
        ledger.write_text(text, encoding="utf-8")

        findings = check_unbounded_candidates(tmp_path, settings)
        assert findings == []

    def test_transient_overage_at_tolerance_boundary(self, tmp_path):
        """Exactly cap + tolerance chars → NOT flagged (≤ check)."""
        settings = _make_settings(tmp_path, max_memory_chars=100)
        ledger = tmp_path / "boundary_memory.md"
        # cap=100, tolerance = max(2, 200) = 200, threshold = 300.
        text = "x" * 300
        assert len(text) == 300
        ledger.write_text(text, encoding="utf-8")

        findings = check_unbounded_candidates(tmp_path, settings)
        assert findings == []

    def test_genuinely_over_cap_finding_with_framing(self, tmp_path):
        """A memory file whose chars exceed cap beyond tolerance IS flagged,
        and the rendered body contains the deployed-.data/code-fix framing,
        chars in the size line, and embedded file contents."""
        from robotsix_mill.runners.data_dir_audit.filing import _build_unbounded_finding

        settings = _make_settings(tmp_path, max_memory_chars=100)
        ledger = tmp_path / "overflow_memory.md"
        text = "x" * 450  # 450 chars > 100 + 200 (tolerance) = 300
        ledger.write_text(text, encoding="utf-8")

        findings = check_unbounded_candidates(tmp_path, settings)

        assert len(findings) == 1
        f = findings[0]
        assert f["pattern"] == "*_memory.md"
        assert f["path"] == "overflow_memory.md"
        assert f["current_size"] == 450  # st_size (bytes, all ASCII)
        assert f["cap_size"] == 100
        assert f["measured_value"] == 450
        assert f["measured_unit"] == "chars"
        assert f["embedded_content"] == text
        assert f["content_truncated"] is False

        # Render the body.
        gap_id, title, body = _build_unbounded_finding(f)

        # gap_id / title unchanged in structure.
        assert gap_id == "unbounded:overflow_memory.md"
        assert "overflow_memory.md" in title

        # Size line expressed in chars.
        assert "450 chars" in body
        assert "450 B" in body  # the byte-size parenthetical

        # Cap line in chars.
        assert "100 chars" in body

        # Deployed-data framing.
        assert "`.data/<repo>/` runtime directory" in body
        assert "not part of the source tree" in body.lower()
        assert "no agent has host data-dir access" in body.lower()
        assert "CODE change" in body

        # Memory-ledger writer named.
        assert "`persist_memory`" in body
        assert "`load_memory`" in body
        assert "`settings.max_memory_chars`" in body

        # File contents section with the embedded text.
        assert "## File contents" in body
        assert "```" in body
        assert text in body

    def test_memory_over_cap_finding_has_embedded_full_content(self, tmp_path):
        """A small over-cap memory file (≤ 20 KB) gets its full content
        embedded in the finding body."""
        from robotsix_mill.runners.data_dir_audit.filing import _build_unbounded_finding

        settings = _make_settings(tmp_path, max_memory_chars=100)
        ledger = tmp_path / "small_overflow_memory.md"
        text = "line 1\nline 2\nline 3\n"
        # 21 chars (all ASCII), cap=100, tolerance=200, 21 ≤ 300 → NOT over cap.
        # Need > 300 chars to flag. Let's make a file with 400 chars.
        text = "line " + "x" * 394 + "\n"  # ~400 chars
        assert len(text) >= 400
        ledger.write_text(text, encoding="utf-8")

        findings = check_unbounded_candidates(tmp_path, settings)
        assert len(findings) == 1
        f = findings[0]

        # Content should be fully embedded (file ≤ 20 KB).
        assert f["embedded_content"] == text
        assert f["content_truncated"] is False

        _gap_id, _title, body = _build_unbounded_finding(f)
        assert text in body
        assert "head+tail excerpt" not in body

    def test_large_file_gets_excerpt_not_full_dump(self, tmp_path):
        """A finding on a file larger than the 20 KB embed threshold
        yields a body whose embed contains a truncation marker and does
        NOT contain the entire content."""
        from robotsix_mill.runners.data_dir_audit.filing import _build_unbounded_finding

        settings = _make_settings(tmp_path, max_memory_chars=100)
        ledger = tmp_path / "big_overflow_memory.md"
        # Create > 20 KB of text. 50000 chars of 'x' = 50000 bytes > 20480.
        text = ("line %05d\n" * 5000) % tuple(range(5000))  # many lines
        # Ensure it's > 20 KB.
        assert len(text.encode("utf-8")) > 20 * 1024
        # Ensure it's over the cap (100 chars + tolerance 200 = 300).
        assert len(text) > 300
        ledger.write_text(text, encoding="utf-8")

        findings = check_unbounded_candidates(tmp_path, settings)
        assert len(findings) == 1
        f = findings[0]

        # Content should be truncated.
        assert f["content_truncated"] is True
        embedded = f["embedded_content"]
        # The truncation marker should be present.
        assert "bytes omitted" in embedded
        # The embedded content should NOT be the full text.
        assert embedded != text
        # It should contain some lines from the beginning.
        assert "line 00000" in embedded
        # And some lines from the end.
        assert "line 04999" in embedded

        # The body note.
        _gap_id, _title, body = _build_unbounded_finding(f)
        assert "head+tail excerpt" in body


class TestRunDataDirAuditPass:
    def test_returns_findings_and_summary(self, tmp_path, monkeypatch):
        """The pass result must surface flagged findings AND reflect
        the count in ``summary``."""
        settings = _make_settings(
            tmp_path,
            max_memory_chars=100,
            data_dir_audit_prune_memory_ledgers=False,
        )
        ledger = tmp_path / "implement_memory.md"
        _write_bytes(ledger, 500)

        # Tests inject Settings via the module-level monkeypatch seam.
        monkeypatch.setattr(
            "robotsix_mill.runners.data_dir_audit.Settings",
            lambda: settings,
        )

        result = run_data_dir_audit_pass()

        assert isinstance(result, DataDirAuditPassResult)
        assert len(result.findings) == 1
        assert result.findings[0]["pattern"] == "*_memory.md"
        # The unbounded segment is appended when findings exist.
        assert "1 unbounded candidate " in result.summary

    def test_no_findings_summary(self, tmp_path, monkeypatch):
        settings = _make_settings(tmp_path)
        monkeypatch.setattr(
            "robotsix_mill.runners.data_dir_audit.Settings",
            lambda: settings,
        )

        result = run_data_dir_audit_pass()

        assert result.findings == []
        # No findings of any kind → zero-finding short-circuit.
        assert "unbounded" not in result.summary
        assert result.summary == "Scanned 0 B in 0 files. No issues found."

    def test_findings_field_defaults_to_empty(self):
        """``DataDirAuditPassResult.findings`` defaults to ``[]``."""
        r = DataDirAuditPassResult(drafts_created=[], summary="no findings")
        assert r.findings == []


# ---------------------------------------------------------------------------
# ticket 7 — Summary output (header line, short-circuit, trimming)
# ---------------------------------------------------------------------------


def test_summary_header_includes_total_bytes_and_file_count(tmp_path, monkeypatch):
    """Header line reports total bytes and (non-zero) file count."""
    s = _make_settings(tmp_path)
    # Seed three non-zero files of known sizes (10/20/30 bytes).
    (s.data_dir / "a.txt").write_bytes(b"x" * 10)
    (s.data_dir / "b.txt").write_bytes(b"y" * 20)
    (s.data_dir / "c.txt").write_bytes(b"z" * 30)

    monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)

    # No board_id in the repo_config → no filing path is reached.
    result = run_data_dir_audit_pass()
    # 60 B total across 3 files; zero findings → single-line short-circuit.
    assert result.summary.startswith("Scanned 60 B in 3 files.")


def test_summary_zero_finding_short_circuit(tmp_path, monkeypatch):
    """Empty data_dir + no findings → single-line short-circuit."""
    s = _make_settings(tmp_path)
    monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)

    result = run_data_dir_audit_pass()
    assert result.summary == "Scanned 0 B in 0 files. No issues found."


def test_summary_truncates_long_paths(tmp_path, monkeypatch):
    """An oversized file with a long flat name gets middle-elided."""
    s = _make_settings(tmp_path)
    # Build a single top-level filename whose ``.data/<rel>`` form
    # exceeds 80 chars — a flat-name oversized file is the only
    # finding, so it's guaranteed to be the largest item picked.
    long_name = "very_long_" + "x" * 100 + ".bin"
    huge = s.data_dir / long_name
    fd = os.open(str(huge), os.O_CREAT | os.O_WRONLY)
    try:
        os.ftruncate(fd, 200 * 1024 * 1024)
    finally:
        os.close(fd)

    monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)

    result = run_data_dir_audit_pass()
    assert "…" in result.summary
    # Sanity: the un-trimmed path should NOT appear verbatim.
    assert long_name not in result.summary


# ---------------------------------------------------------------------------
# Session/repo passthrough
# ---------------------------------------------------------------------------


def test_run_pass_propagates_session_id(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.runners.data_dir_audit.Settings",
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


def test_scan_board_sizes_excludes_sqlite_transient_sidecars(tmp_path):
    """SQLite WAL/SHM/journal sidecars are skipped so their normal
    checkpoint fluctuation never produces growth findings."""
    board = tmp_path / "board"
    board.mkdir()
    (board / "mill.db").write_bytes(b"x" * 100)
    (board / "mill.db-wal").write_bytes(b"x" * 5000)
    (board / "mill.db-shm").write_bytes(b"x" * 3000)
    (board / "mill.db-journal").write_bytes(b"x" * 2000)

    sizes = _scan_board_sizes(board)

    assert sizes["mill.db"]["size_bytes"] == 100
    assert "mill.db-wal" not in sizes
    assert "mill.db-shm" not in sizes
    assert "mill.db-journal" not in sizes


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
                data_dir_audit_growth_delta_pct_min_bytes=0,
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
                data_dir_audit_growth_delta_pct_min_bytes=0,
            ),
        )
        assert len(flags) == 1
        assert flags[0]["path"] == "d/"
        assert flags[0]["threshold_exceeded"] == "both"

    def test_pct_met_but_below_min_delta_no_flag(self):
        """pct threshold met but delta_bytes below min_abs → no flag."""
        prior = {"f": {"size_bytes": 100, "mtime": 1}}
        current = {"f": {"size_bytes": 200, "mtime": 2}}
        flags = _compute_growth_deltas(
            prior,
            current,
            _s(
                data_dir_audit_growth_delta_bytes=10_000_000,
                data_dir_audit_growth_delta_pct=20,
                data_dir_audit_growth_delta_pct_min_bytes=1_048_576,
            ),
        )
        assert flags == []

    def test_pct_met_and_delta_above_min_flagged(self):
        """pct threshold met and delta_bytes >= min_abs → flagged."""
        prior = {"f": {"size_bytes": 100, "mtime": 1}}
        current = {"f": {"size_bytes": 1_200_000, "mtime": 2}}
        flags = _compute_growth_deltas(
            prior,
            current,
            _s(
                data_dir_audit_growth_delta_bytes=10_000_000,
                data_dir_audit_growth_delta_pct=20,
                data_dir_audit_growth_delta_pct_min_bytes=1_048_576,
            ),
        )
        assert len(flags) == 1
        assert flags[0]["threshold_exceeded"] == "pct"

    def test_bytes_path_unaffected_by_min_delta(self):
        """A sub-threshold-delta file whose absolute delta still meets
        the bytes threshold flags via the bytes path regardless of the
        min-delta guard."""
        prior = {"f": {"size_bytes": 100, "mtime": 1}}
        current = {"f": {"size_bytes": 15_000_000, "mtime": 2}}
        flags = _compute_growth_deltas(
            prior,
            current,
            _s(
                data_dir_audit_growth_delta_bytes=10_000_000,
                data_dir_audit_growth_delta_pct=9999,
                data_dir_audit_growth_delta_pct_min_bytes=1_048_576,
            ),
        )
        assert len(flags) == 1
        # bytes path fires regardless of floor (pct may also fire since
        # 15 MB > 1 MiB floor, producing "both" — either is fine).
        assert flags[0]["threshold_exceeded"] in ("bytes", "both")


# ---------------------------------------------------------------------------
# Unit: _classify_growth_path — mill.db classification
# ---------------------------------------------------------------------------


def test_classify_mill_db_bounded():
    """_classify_growth_path returns _GROWTH_CLASS_DB for mill.db."""
    cls = _classify_growth_path("mill.db", {})
    assert cls == _GROWTH_CLASS_DB
    # The _self_healing predicate maps non-OTHER → True.
    assert cls != _GROWTH_CLASS_OTHER


def test_classify_mill_db_with_tickets_irrelevant():
    """mill.db is classified by path alone — ticket_states don't
    affect it (the tid is None path wins)."""
    # Even with a populated ticket_states dict, mill.db is recognized.
    cls = _classify_growth_path("mill.db", {"some-ticket": "active"})
    assert cls == _GROWTH_CLASS_DB


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
    monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)

    board = s.data_dir / "test-board"
    _seed_board(board)

    result = run_data_dir_audit_pass()
    assert isinstance(result, DataDirAuditPassResult)
    assert result.growth_flags == []
    # First run + no other findings → zero-finding short-circuit.
    assert "No issues found." in result.summary

    # State file should exist now
    state_path = _growth_state_path(s, "test-board")
    assert state_path.exists()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "big.log" in state
    assert state["big.log"]["size_bytes"] == 100


def test_growth_detected_on_second_run(tmp_path, monkeypatch):
    """Second run picks up prior state and flags growth."""
    s = _make_settings(tmp_path)
    monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)

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
    assert "1 growth flag " in result.summary
    assert " grew by " in result.summary


def test_growth_ignores_shrink(tmp_path, monkeypatch):
    """Shrinking files should not be flagged."""
    s = _make_settings(tmp_path)
    monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)

    board = s.data_dir / "test-board"
    _seed_board(board)
    (board / "big.log").write_bytes(b"x" * 10_000_000)

    run_data_dir_audit_pass()

    # Shrink the file
    (board / "big.log").write_bytes(b"x" * 1_000)

    result = run_data_dir_audit_pass()
    assert result.growth_flags == []
    # No findings of any kind after the shrink → zero-finding short-circuit.
    assert "No issues found." in result.summary


def test_corrupt_state_recovered(tmp_path, monkeypatch, caplog):
    """Corrupt state file should be treated as first-run and overwritten."""
    s = _make_settings(tmp_path)
    monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)

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
    monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)

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
    # Growth line uses the new shape: count + parenthetical largest.
    assert " growth flag" in result.summary
    assert " grew by " in result.summary


def test_result_summary_format(tmp_path, monkeypatch):
    """Verify the summary string format."""
    s = _make_settings(tmp_path)
    monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)

    board = s.data_dir / "test-board"
    _seed_board(board)
    run_data_dir_audit_pass()
    (board / "big.log").write_bytes(b"x" * 30_000_000)

    result = run_data_dir_audit_pass()
    # New layout: multi-line summary headed by "Scanned ..." with the
    # growth category contributing a per-line entry.
    assert result.summary.startswith("Scanned ")
    assert "\n" in result.summary
    assert result.session_id == ""  # not set by default


# ---------------------------------------------------------------------------
# ticket 6 — Filing & cross-pass dedup via gap-id markers
# ---------------------------------------------------------------------------


class TestFilingAndDedup:
    """Filing logic + dedup behaviour for ``_file_findings_as_tickets``
    and its integration through ``run_data_dir_audit_pass``."""

    def _seed_one_of_each_finding(self, s: Settings):
        """Plant one oversized file, one unbounded ``runs.json``, one
        orphan workspace, and one growth scenario under ``s.data_dir``.

        Pre-seeds the growth-state file so the growth check fires on
        the FIRST pass (no warm-up run needed).

        Returns the board id used for orphan + growth setup.
        """
        board_id = "test-board"
        db.init_db(s, board_id)

        # Oversized file: 200 MiB > 100 MiB threshold.
        fd = os.open(str(s.data_dir / "huge.bin"), os.O_CREAT | os.O_WRONLY)
        try:
            os.ftruncate(fd, 200 * 1024 * 1024)
        finally:
            os.close(fd)

        # Unbounded runs.json: 60 entries > MAX_ENTRIES (50).
        runs = s.data_dir / "runs.json"
        runs.write_text(json.dumps([{"id": i} for i in range(60)]))

        # Orphan workspace under board with no DB row.
        _make_workspace_dir(s, board_id, "20260101T000000Z-orph-aa11")

        # Growth scenario: write the file at its CURRENT (grown) size,
        # then pre-seed the state file with a small prior size so the
        # growth check fires on the first pass.
        big = s.data_dir / board_id / "big.log"
        big.parent.mkdir(parents=True, exist_ok=True)
        big.write_bytes(b"x" * 20_000_000)
        state_path = _growth_state_path(s, board_id)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps({"big.log": {"size_bytes": 100, "mtime": 1.0}}),
            encoding="utf-8",
        )
        return board_id

    def _run_with_filing(self, s, monkeypatch, *, session_id=""):
        monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
        return run_data_dir_audit_pass(
            session_id=session_id, repo_config=_test_repo_config()
        )

    def test_each_finding_produces_one_draft(self, tmp_path, monkeypatch):
        """One oversized + one unbounded + one growth →
        at least 3 drafts created, with at least one per issue type.
        (Orphans are GC'd, not filed.)"""
        s = _make_settings(tmp_path, data_dir_audit_max_drafts_per_pass=20)
        self._seed_one_of_each_finding(s)

        result = self._run_with_filing(s, monkeypatch)

        titles = [d["title"] for d in result.drafts_created]
        prefixes = {
            "growth",
            "oversized",
            "unbounded",
        }
        seen = set()
        for title in titles:
            for prefix in prefixes:
                if title.startswith(prefix):
                    seen.add(prefix)
        assert seen == prefixes, titles
        # Exactly one per issue type (plus possibly a couple of incidental
        # growth flags from the workspaces/ dir as it accrues content).
        oversized_count = sum(1 for t in titles if t.startswith("oversized"))
        unbounded_count = sum(1 for t in titles if t.startswith("unbounded"))
        assert oversized_count == 1, titles
        assert unbounded_count == 1, titles
        db.reset_engine()

    def test_second_pass_dedups_in_flight_tickets(self, tmp_path, monkeypatch):
        """Same findings → second pass files no new drafts; no
        duplicate gap_ids land in the DB."""
        s = _make_settings(tmp_path, data_dir_audit_max_drafts_per_pass=10)
        db.init_db(s, "test-board")
        # Plant a single oversized file (simplest finding).
        fd = os.open(str(s.data_dir / "huge.bin"), os.O_CREAT | os.O_WRONLY)
        try:
            os.ftruncate(fd, 200 * 1024 * 1024)
        finally:
            os.close(fd)

        first = self._run_with_filing(s, monkeypatch)
        assert len(first.drafts_created) == 1

        second = self._run_with_filing(s, monkeypatch)
        assert second.drafts_created == []

        # Confirm only one ticket for this gap_id in the DB.
        service = TicketService(s, board_id="test-board")
        gap_ids: list[str] = []
        for t in service.list():
            if t.source != SourceKind.DATA_DIR_AUDIT:
                continue
            body = service.workspace(t).read_description()
            for m in _GAP_ID_RE.finditer(body):
                if m.group(1) == "data_dir_audit":
                    gap_ids.append(m.group(2))
        assert gap_ids == ["oversized:huge.bin"]
        db.reset_engine()

    def test_resolved_ticket_allows_refile(self, tmp_path, monkeypatch):
        """Closed/declined tickets do NOT block refiling when the
        on-disk finding still exists."""
        s = _make_settings(tmp_path, data_dir_audit_max_drafts_per_pass=10)
        db.init_db(s, "test-board")
        fd = os.open(str(s.data_dir / "huge.bin"), os.O_CREAT | os.O_WRONLY)
        try:
            os.ftruncate(fd, 200 * 1024 * 1024)
        finally:
            os.close(fd)

        first = self._run_with_filing(s, monkeypatch)
        assert len(first.drafts_created) == 1
        first_id = first.drafts_created[0]["id"]

        # Close the ticket directly (DRAFT → CLOSED = declined).
        service = TicketService(s, board_id="test-board")
        service.transition(first_id, State.CLOSED, note="declined as noise")

        # Finding still on disk → a new draft should be filed.
        second = self._run_with_filing(s, monkeypatch)
        assert len(second.drafts_created) == 1
        assert second.drafts_created[0]["id"] != first_id
        db.reset_engine()

    def test_disappeared_finding_does_not_refile(self, tmp_path, monkeypatch):
        """A finding that no longer reproduces produces no draft on
        the next pass (vacuously dedup'd)."""
        s = _make_settings(tmp_path, data_dir_audit_max_drafts_per_pass=10)
        db.init_db(s, "test-board")
        huge = s.data_dir / "huge.bin"
        fd = os.open(str(huge), os.O_CREAT | os.O_WRONLY)
        try:
            os.ftruncate(fd, 200 * 1024 * 1024)
        finally:
            os.close(fd)

        first = self._run_with_filing(s, monkeypatch)
        assert len(first.drafts_created) == 1

        huge.unlink()

        second = self._run_with_filing(s, monkeypatch)
        assert second.drafts_created == []
        db.reset_engine()

    def test_per_pass_cap_enforced(self, tmp_path, monkeypatch):
        """With 10 oversized files and a cap of 3, exactly 3 drafts are
        filed — the three largest, in size-desc order."""
        s = _make_settings(tmp_path, data_dir_audit_max_drafts_per_pass=3)
        db.init_db(s, "test-board")
        sizes = []
        for i in range(10):
            size = (200 + i) * 1024 * 1024  # 200..209 MiB
            fp = s.data_dir / f"big_{i:02d}.bin"
            fd = os.open(str(fp), os.O_CREAT | os.O_WRONLY)
            try:
                os.ftruncate(fd, size)
            finally:
                os.close(fd)
            sizes.append((f"big_{i:02d}.bin", size))

        result = self._run_with_filing(s, monkeypatch)

        assert len(result.drafts_created) == 3
        # Top-N largest items is 10 here, so all 10 are candidates;
        # the 3 chosen must be the largest in size_bytes desc.
        chosen = [d["title"] for d in result.drafts_created]
        # Three biggest filenames: big_09 (209 MiB), big_08, big_07.
        assert any("big_09.bin" in t for t in chosen), chosen
        assert any("big_08.bin" in t for t in chosen), chosen
        assert any("big_07.bin" in t for t in chosen), chosen
        db.reset_engine()

    def test_gap_id_marker_present_in_body(self, tmp_path, monkeypatch):
        """Each created ticket body contains a parseable gap-id
        marker matching ``_GAP_ID_RE`` with label ``data_dir_audit``."""
        s = _make_settings(tmp_path, data_dir_audit_max_drafts_per_pass=10)
        db.init_db(s, "test-board")
        fd = os.open(str(s.data_dir / "huge.bin"), os.O_CREAT | os.O_WRONLY)
        try:
            os.ftruncate(fd, 200 * 1024 * 1024)
        finally:
            os.close(fd)

        result = self._run_with_filing(s, monkeypatch)
        assert len(result.drafts_created) == 1
        service = TicketService(s, board_id="test-board")
        ticket = service.get(result.drafts_created[0]["id"])
        assert ticket is not None
        body = service.workspace(ticket).read_description()
        match = _GAP_ID_RE.search(body)
        assert match is not None
        assert match.group(1) == "data_dir_audit"
        assert match.group(2) == "oversized:huge.bin"
        db.reset_engine()

    def test_no_repo_config_skips_filing(self, tmp_path, monkeypatch):
        """When ``repo_config`` is None, findings are produced but no
        filing happens — the summary still describes them."""
        s = _make_settings(tmp_path)
        fd = os.open(str(s.data_dir / "huge.bin"), os.O_CREAT | os.O_WRONLY)
        try:
            os.ftruncate(fd, 200 * 1024 * 1024)
        finally:
            os.close(fd)

        monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)

        result = run_data_dir_audit_pass(repo_config=None)

        assert result.drafts_created == []
        assert len(result.oversized_items) == 1
        assert "1 oversized item " in result.summary
        db.reset_engine()

    def test_create_failure_does_not_abort_pass(self, tmp_path, monkeypatch):
        """A failing ``TicketService.create`` is logged via
        ``log.exception`` but the loop continues to the next finding."""
        s = _make_settings(tmp_path, data_dir_audit_max_drafts_per_pass=10)
        db.init_db(s, "test-board")
        # Three oversized files → three filing attempts.
        for i in range(3):
            size = (200 + i) * 1024 * 1024
            fp = s.data_dir / f"big_{i:02d}.bin"
            fd = os.open(str(fp), os.O_CREAT | os.O_WRONLY)
            try:
                os.ftruncate(fd, size)
            finally:
                os.close(fd)

        # Make every other create() raise so we exercise both branches:
        # the first succeeds, the second raises (logged), the third
        # succeeds. The pass must return both successful tickets, not
        # abort on the failure.
        real_create = TicketService.create
        call_count = {"n": 0}

        def flaky_create(self, *a, **kw):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("synthetic create failure")
            return real_create(self, *a, **kw)

        monkeypatch.setattr(TicketService, "create", flaky_create)

        with caplog_at_level_module(s, monkeypatch) as caplog:
            result = self._run_with_filing(s, monkeypatch)

        # 3 attempts, 1 failure → 2 successful drafts.
        assert call_count["n"] == 3
        assert len(result.drafts_created) == 2
        assert any(
            "failed to create draft ticket" in rec.message for rec in caplog.records
        )
        db.reset_engine()


class _CaplogCM:
    """Tiny caplog-like context manager — pytest's caplog fixture is
    function-scoped and can't be cleanly injected into a class helper,
    so we attach a handler ourselves."""

    def __init__(self):
        self.records: list[logging.LogRecord] = []
        self._logger = logging.getLogger("robotsix_mill.data_dir_audit")
        self._handler: logging.Handler | None = None
        self._prev_level = self._logger.level

    def __enter__(self):
        class _H(logging.Handler):
            def __init__(self, sink):
                super().__init__()
                self.sink = sink

            def emit(self, record):
                self.sink.records.append(record)

        self._handler = _H(self)
        self._logger.addHandler(self._handler)
        self._logger.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        if self._handler is not None:
            self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._prev_level)


def caplog_at_level_module(_s, _monkeypatch):
    """Module-scope caplog substitute for inside-class use."""
    return _CaplogCM()


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


# ---------------------------------------------------------------------------
# prune_closed GC — workspaces of terminal-state tickets
# ---------------------------------------------------------------------------


def test_prune_closed_knob_default_off_is_noop(tmp_path, monkeypatch):
    """With ``data_dir_audit_prune_closed`` unset/false, an old CLOSED
    ticket's workspace is NOT removed by the pass."""
    s = _make_settings(tmp_path)
    ws = _make_workspace_dir(s, "board-x", "20260101T000000Z-closed-aaaa")
    _insert_closed_ticket(
        s,
        "board-x",
        "20260101T000000Z-closed-aaaa",
        closed_at=_now() - timedelta(days=30),
    )

    monkeypatch.setattr(
        "robotsix_mill.runners.data_dir_audit.Settings",
        lambda: s,
    )

    result = run_data_dir_audit_pass()

    assert ws.exists()
    assert result.closed_pruned == 0
    db.reset_engine()


def test_prune_closed_removes_old_closed_workspace(tmp_path):
    """With the age threshold low, a CLOSED-ticket workspace older than
    the threshold is removed and the return count reflects it."""
    s = _make_settings(
        tmp_path,
        data_dir_audit_prune_closed=True,
        data_dir_audit_prune_closed_age_seconds=0,
    )
    ws = _make_workspace_dir(s, "board-x", "20260101T000000Z-closed-bbbb")
    _insert_closed_ticket(
        s,
        "board-x",
        "20260101T000000Z-closed-bbbb",
        closed_at=_now() - timedelta(days=30),
    )

    removed = _prune_closed_workspaces(s)

    assert removed == 1
    assert not ws.exists()
    db.reset_engine()


def test_prune_closed_age_guard_keeps_recent_closure(tmp_path):
    """With a 7-day threshold, a ticket closed 'now' is NOT removed."""
    s = _make_settings(
        tmp_path,
        data_dir_audit_prune_closed=True,
        data_dir_audit_prune_closed_age_seconds=604_800,
    )
    ws = _make_workspace_dir(s, "board-x", "20260101T000000Z-closed-cccc")
    _insert_closed_ticket(
        s,
        "board-x",
        "20260101T000000Z-closed-cccc",
        closed_at=_now(),
    )

    removed = _prune_closed_workspaces(s)

    assert removed == 0
    assert ws.exists()
    db.reset_engine()


def test_prune_closed_leaves_non_terminal_workspaces(tmp_path):
    """DRAFT, DONE, and BLOCKED ticket workspaces are never pruned."""
    s = _make_settings(
        tmp_path,
        data_dir_audit_prune_closed=True,
        data_dir_audit_prune_closed_age_seconds=0,
    )
    ws_draft = _make_workspace_dir(s, "board-x", "20260101T000000Z-draft-0001")
    ws_done = _make_workspace_dir(s, "board-x", "20260101T000000Z-done-0002")
    ws_blocked = _make_workspace_dir(s, "board-x", "20260101T000000Z-blkd-0003")
    _insert_ticket_with_state(s, "board-x", "20260101T000000Z-draft-0001", State.DRAFT)
    _insert_ticket_with_state(s, "board-x", "20260101T000000Z-done-0002", State.DONE)
    _insert_ticket_with_state(s, "board-x", "20260101T000000Z-blkd-0003", State.BLOCKED)

    removed = _prune_closed_workspaces(s)

    assert removed == 0
    assert ws_draft.exists()
    assert ws_done.exists()
    assert ws_blocked.exists()
    db.reset_engine()


def test_prune_closed_leaves_orphans(tmp_path):
    """A workspace dir with no matching ticket row is left for the
    orphan detector, not removed by the prune step."""
    s = _make_settings(
        tmp_path,
        data_dir_audit_prune_closed=True,
        data_dir_audit_prune_closed_age_seconds=0,
    )
    ws = _make_workspace_dir(s, "board-x", "20260101T000000Z-orph-dddd")
    db.init_db(s, "board-x")  # board DB exists, but ticket row does not

    removed = _prune_closed_workspaces(s)

    assert removed == 0
    assert ws.exists()
    db.reset_engine()


def test_prune_closed_board_failure_is_skipped(tmp_path, monkeypatch, caplog):
    """A board whose DB access raises is skipped with a warning while a
    second healthy board is still processed — no exception propagates."""
    s = _make_settings(
        tmp_path,
        data_dir_audit_prune_closed=True,
        data_dir_audit_prune_closed_age_seconds=0,
    )
    ws_a = _make_workspace_dir(s, "board-a", "20260101T000000Z-closed-aa11")
    ws_b = _make_workspace_dir(s, "board-b", "20260101T000000Z-closed-bb22")
    _insert_closed_ticket(
        s,
        "board-a",
        "20260101T000000Z-closed-aa11",
        closed_at=_now() - timedelta(days=30),
    )
    db.init_db(s, "board-b")  # discoverable board with a workspace candidate

    real_session = db.session

    def fake_session(settings, board_id):
        if board_id == "board-b":
            raise RuntimeError("simulated unreachable DB")
        return real_session(settings, board_id)

    monkeypatch.setattr(db, "session", fake_session)

    with caplog.at_level(logging.WARNING, logger="robotsix_mill.data_dir_audit"):
        removed = _prune_closed_workspaces(s)

    assert removed == 1
    assert not ws_a.exists()  # healthy board pruned
    assert ws_b.exists()  # failing board untouched
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    db.reset_engine()


def test_pass_alert_only_after_gc(tmp_path, monkeypatch):
    """A large CLOSED-ticket workspace produces no oversized finding —
    whether or not GC pruning runs. With pruning ON it is physically GC'd
    before measurement; with pruning OFF it remains on disk but is still
    suppressed from the oversized check (a workspace's size is transient
    infra, never an actionable oversized alert). The GC knob governs
    reclamation, not oversized reporting."""

    def _build_settings(*, prune: bool) -> Settings:
        root = tmp_path / ("on" if prune else "off")
        st = _make_settings(
            root,
            data_dir_audit_prune_closed=prune,
            data_dir_audit_prune_closed_age_seconds=0,
            data_dir_audit_size_threshold_bytes=1_000_000,
        )
        ws = _make_workspace_dir(st, "board-x", "20260101T000000Z-closed-eeee")
        _write_bytes(ws / "big.bin", 2_000_000)
        _insert_closed_ticket(
            st,
            "board-x",
            "20260101T000000Z-closed-eeee",
            closed_at=_now() - timedelta(days=30),
        )
        return st

    # Pruning ENABLED → workspace GC'd before measurement → no oversized.
    s_on = _build_settings(prune=True)
    monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s_on)
    result_on = run_data_dir_audit_pass()
    assert result_on.closed_pruned == 1
    assert result_on.oversized_items == []
    db.reset_engine()

    # Pruning DISABLED → workspace remains on disk, but is STILL suppressed
    # from the oversized check (workspace infra is never an oversized alert).
    s_off = _build_settings(prune=False)
    monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s_off)
    result_off = run_data_dir_audit_pass()
    assert result_off.closed_pruned == 0
    assert not any(
        item["path"].endswith("big.bin") for item in result_off.oversized_items
    )
    db.reset_engine()


# ---------------------------------------------------------------------------
# Active-workspace growth-flag suppression
# ---------------------------------------------------------------------------


def _grow_workspace_file(settings: Settings, board_id: str, ticket_id: str) -> None:
    """Grow a file inside ``<board>/workspaces/<ticket_id>/`` so the
    second audit pass observes a large delta on that workspace dir."""
    _write_bytes(
        settings.workspaces_dir_for(board_id) / ticket_id / "repo.bin",
        20_000_000,
    )


def test_workspace_ticket_id_for_path():
    """Unit cases for ``_workspace_ticket_id_for_path``."""
    tid = "20260101T000000Z-active-ab12"
    assert _workspace_ticket_id_for_path(f"workspaces/{tid}/repo/.git/objects/") == tid
    assert _workspace_ticket_id_for_path(f"workspaces/{tid}") == tid
    # Non-workspace path → None.
    assert _workspace_ticket_id_for_path("big.log") is None
    # Second segment not a ticket-ID prefix → None.
    assert _workspace_ticket_id_for_path("workspaces/not-a-ticket/repo") is None


def test_growth_suppressed_for_active_workspace(tmp_path, monkeypatch, caplog):
    """Growth inside an active (live, non-terminal) ticket workspace is
    suppressed and an INFO line is logged."""
    s = _make_settings(tmp_path)
    monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
    board_id = "test-board"
    ticket_id = "20260101T000000Z-active-ab12"
    _make_workspace_dir(s, board_id, ticket_id)
    _insert_ticket(s, board_id, ticket_id)  # DRAFT / active; creates mill.db

    run_data_dir_audit_pass()  # baseline
    _grow_workspace_file(s, board_id, ticket_id)

    with caplog.at_level(logging.INFO, logger="robotsix_mill.data_dir_audit"):
        result = run_data_dir_audit_pass()

    ws_prefix = f"workspaces/{ticket_id}"
    assert not any(f["path"].startswith(ws_prefix) for f in result.growth_flags)
    assert any(
        "suppressing growth flag" in rec.message
        and "active ticket workspace" in rec.message
        for rec in caplog.records
    )
    db.reset_engine()


def test_growth_suppressed_for_terminal_workspace_when_clone_gc_on(
    tmp_path, monkeypatch
):
    """With the (default-on) terminal-clone GC, growth inside a
    terminal-ticket workspace is suppressed — the GC reclaims it on
    the next pass instead of filing an unactionable ticket."""
    s = _make_settings(tmp_path)
    monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
    board_id = "test-board"
    ticket_id = "20260101T000000Z-closed-bbbb"
    _make_workspace_dir(s, board_id, ticket_id)
    _insert_closed_ticket(
        s,
        board_id,
        ticket_id,
        closed_at=_now() - timedelta(days=30),
        state=State.CLOSED,
    )

    run_data_dir_audit_pass()  # baseline
    _grow_workspace_file(s, board_id, ticket_id)

    result = run_data_dir_audit_pass()

    ws_prefix = f"workspaces/{ticket_id}"
    assert not any(f["path"].startswith(ws_prefix) for f in result.growth_flags)
    db.reset_engine()


def test_growth_kept_for_terminal_workspace_when_clone_gc_off(tmp_path, monkeypatch):
    """With the terminal-clone GC disabled, terminal-workspace growth
    still flags (nothing reclaims it)."""
    s = _make_settings(tmp_path, data_dir_audit_prune_terminal_clones=False)
    monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
    board_id = "test-board"
    ticket_id = "20260101T000000Z-closed-dddd"
    _make_workspace_dir(s, board_id, ticket_id)
    _insert_closed_ticket(
        s,
        board_id,
        ticket_id,
        closed_at=_now() - timedelta(days=30),
        state=State.CLOSED,
    )

    run_data_dir_audit_pass()  # baseline
    _grow_workspace_file(s, board_id, ticket_id)

    result = run_data_dir_audit_pass()

    ws_prefix = f"workspaces/{ticket_id}"
    assert any(f["path"].startswith(ws_prefix) for f in result.growth_flags)
    db.reset_engine()


def _grow_periodic_workspace_file(settings: Settings, board_id: str) -> None:
    """Grow a file inside a periodic-pass clone path
    (``<board>/health_workspace/repo/``) so the second audit pass sees a
    large delta on that periodic-pass clone."""
    _write_bytes(
        settings.data_dir / board_id / "health_workspace" / "repo" / "objects.bin",
        20_000_000,
    )


def test_is_periodic_pass_workspace_path():
    """Unit cases for ``_is_periodic_pass_workspace_path``."""
    assert _is_periodic_pass_workspace_path("health_workspace/repo/.git/objects/")
    assert _is_periodic_pass_workspace_path("survey_workspace/repo")
    # Aggregate periodic workspace clone (persists across ticks).
    assert _is_periodic_pass_workspace_path("periodic_workspace/repo/src/foo/")
    # Plain top-level file → False.
    assert not _is_periodic_pass_workspace_path("big.log")
    # Per-ticket workspace path → False (handled by the b844 branch).
    assert not _is_periodic_pass_workspace_path(
        "workspaces/20260101T000000Z-active-ab12/repo"
    )


def test_is_meta_clone_cache_path():
    """Unit cases for ``_is_meta_clone_cache_path``."""
    # Aggregate meta board clone cache dir.
    assert _is_meta_clone_cache_path("workspace/")
    # Per-repo clone inside the cache.
    assert _is_meta_clone_cache_path("workspace/robotsix-auto-mail/")
    assert _is_meta_clone_cache_path("workspace/robotsix-auto-mail/repo/src/foo.py")
    # Plain top-level file → False.
    assert not _is_meta_clone_cache_path("big.log")
    # Per-ticket ``workspaces/`` (plural) is NOT the meta clone cache.
    assert not _is_meta_clone_cache_path("workspaces/20260101T000000Z-active-ab12/repo")
    assert not _is_meta_clone_cache_path("health_workspace/repo")


def test_growth_suppressed_for_meta_clone_cache(tmp_path, monkeypatch, caplog):
    """Growth inside the meta board clone cache (``workspace/...``) is
    suppressed unconditionally and an INFO line is logged."""
    s = _make_settings(tmp_path)
    monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
    board_id = "meta"
    db.init_db(s, board_id)
    # Seed a small clone file so the cache path exists in the baseline
    # snapshot; the second pass then observes a large delta.
    _write_bytes(
        s.data_dir / board_id / "workspace" / "robotsix-auto-mail" / "objects.bin",
        100,
    )

    run_data_dir_audit_pass()  # baseline
    _write_bytes(
        s.data_dir / board_id / "workspace" / "robotsix-auto-mail" / "objects.bin",
        20_000_000,
    )

    with caplog.at_level(logging.INFO, logger="robotsix_mill.data_dir_audit"):
        result = run_data_dir_audit_pass()

    assert not any(f["path"].startswith("workspace/") for f in result.growth_flags)
    assert any(
        "suppressing growth flag" in rec.message
        and "meta board clone cache" in rec.message
        for rec in caplog.records
    )
    db.reset_engine()


def test_growth_suppressed_for_periodic_pass_workspace(tmp_path, monkeypatch, caplog):
    """Growth inside a periodic-pass clone (``health_workspace/repo/``)
    is suppressed unconditionally and an INFO line is logged."""
    s = _make_settings(tmp_path)
    monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
    board_id = "test-board"
    db.init_db(s, board_id)
    # Seed a small clone file so the periodic-pass path exists in the
    # baseline snapshot; the second pass then observes a large delta.
    _write_bytes(
        s.data_dir / board_id / "health_workspace" / "repo" / "objects.bin",
        100,
    )

    run_data_dir_audit_pass()  # baseline
    _grow_periodic_workspace_file(s, board_id)

    with caplog.at_level(logging.INFO, logger="robotsix_mill.data_dir_audit"):
        result = run_data_dir_audit_pass()

    assert not any(
        f["path"].startswith("health_workspace/") for f in result.growth_flags
    )
    assert any(
        "suppressing growth flag" in rec.message
        and "periodic-pass clone" in rec.message
        for rec in caplog.records
    )
    db.reset_engine()


def test_growth_suppressed_for_memory_ledger(tmp_path, monkeypatch, caplog):
    """Growth in a ``*_memory.md`` ledger file is suppressed and an INFO
    line is logged, since these are bounded by ``max_memory_chars``."""
    s = _make_settings(
        tmp_path,
        data_dir_audit_prune_memory_ledgers=False,
    )
    monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
    board_id = "test-board"
    db.init_db(s, board_id)
    # Seed a small memory ledger so it exists in the baseline snapshot.
    _write_bytes(
        s.data_dir / board_id / "copy_paste_memory.md",
        100,
    )

    run_data_dir_audit_pass()  # baseline
    _write_bytes(
        s.data_dir / board_id / "copy_paste_memory.md",
        15_000_000,
    )

    with caplog.at_level(logging.INFO, logger="robotsix_mill.data_dir_audit"):
        result = run_data_dir_audit_pass()

    assert not any(f["path"] == "copy_paste_memory.md" for f in result.growth_flags)
    assert any(
        "suppressing growth flag" in rec.message
        and "bounded memory ledger" in rec.message
        for rec in caplog.records
    )
    db.reset_engine()


def test_growth_suppressed_for_run_registry(tmp_path, monkeypatch, caplog):
    """Growth in ``runs.json`` is suppressed and an INFO line is
    logged, since the RunRegistry caps it at 50 entries."""
    s = _make_settings(tmp_path)
    monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
    board_id = "test-board"
    db.init_db(s, board_id)
    # Seed a small runs.json so it exists in the baseline snapshot.
    _write_bytes(
        s.data_dir / board_id / "runs.json",
        100,
    )

    run_data_dir_audit_pass()  # baseline
    _write_bytes(
        s.data_dir / board_id / "runs.json",
        15_000_000,
    )

    with caplog.at_level(logging.INFO, logger="robotsix_mill.data_dir_audit"):
        result = run_data_dir_audit_pass()

    assert not any(f["path"] == "runs.json" for f in result.growth_flags)
    assert any(
        "suppressing growth flag" in rec.message
        and "bounded run registry" in rec.message
        for rec in caplog.records
    )
    db.reset_engine()


def test_growth_suppressed_for_candidates_ledger(tmp_path, monkeypatch, caplog):
    """Growth in ``AGENT_CANDIDATES.md`` is suppressed and an INFO line
    is logged, since ``prune_candidates`` caps it at
    ``retrospect_candidates_max_entries``."""
    s = _make_settings(tmp_path)
    monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
    board_id = "test-board"
    db.init_db(s, board_id)
    # Seed a small candidates file so it exists in the baseline snapshot.
    _write_bytes(
        s.data_dir / board_id / "AGENT_CANDIDATES.md",
        100,
    )

    run_data_dir_audit_pass()  # baseline
    _write_bytes(
        s.data_dir / board_id / "AGENT_CANDIDATES.md",
        15_000_000,
    )

    with caplog.at_level(logging.INFO, logger="robotsix_mill.data_dir_audit"):
        result = run_data_dir_audit_pass()

    assert not any(f["path"] == "AGENT_CANDIDATES.md" for f in result.growth_flags)
    assert any(
        "suppressing growth flag" in rec.message
        and "bounded candidates ledger" in rec.message
        for rec in caplog.records
    )
    db.reset_engine()


def test_cross_class_cap_counts_across_classes(tmp_path):
    """A single ``data_dir_audit_max_drafts_per_pass`` cap counts across
    a mixed-class finding set (proving the cap is global, not per-class).

    With 2 oversized + 2 growth + 2 unbounded findings and a cap of 3,
    exactly 3 drafts are created — drawn in ``_order_findings`` priority
    (growth → oversized → unbounded), so the result spans more than one
    class."""
    s = _make_settings(tmp_path, data_dir_audit_max_drafts_per_pass=3)
    db.init_db(s, "test-board")
    service = TicketService(s, board_id="test-board")

    oversized = [
        {"path": "big_a.bin", "size_bytes": 300 * 1024 * 1024, "is_directory": False},
        {"path": "big_b.bin", "size_bytes": 250 * 1024 * 1024, "is_directory": False},
    ]
    growth_flags = [
        {
            "check": "growth_delta",
            "path": "grow_a.log",
            "board_id": "test-board",
            "current_size_bytes": 50_000_000,
            "prior_size_bytes": 1_000_000,
            "delta_bytes": 49_000_000,
            "delta_pct": 4900.0,
            "threshold_exceeded": "both",
        },
        {
            "check": "growth_delta",
            "path": "grow_b.log",
            "board_id": "test-board",
            "current_size_bytes": 40_000_000,
            "prior_size_bytes": 1_000_000,
            "delta_bytes": 39_000_000,
            "delta_pct": 3900.0,
            "threshold_exceeded": "both",
        },
    ]
    unbounded = [
        {
            "check": "unbounded_candidates",
            "path": "reg_a.json",
            "current_size": 6 * 1024 * 1024,
            "cap_size": 5 * 1024 * 1024,
            "cap_detail": "default=5 MB",
            "pattern": "*.json",
            "record_count": None,
            "record_max": None,
        },
        {
            "check": "unbounded_candidates",
            "path": "reg_b.json",
            "current_size": 7 * 1024 * 1024,
            "cap_size": 5 * 1024 * 1024,
            "cap_detail": "default=5 MB",
            "pattern": "*.json",
            "record_count": None,
            "record_max": None,
        },
    ]

    created = _file_findings_as_tickets(
        s,
        service,
        oversized,
        growth_flags,
        unbounded,
    )

    # Exactly the global cap, NOT cap-per-class (which would be 6).
    assert len(created) == 3
    titles = [d["title"] for d in created]
    # Priority order: both growth flags first (delta desc), then the
    # largest oversized — proving the cap drew across classes.
    assert sum(1 for t in titles if t.startswith("growth")) == 2
    assert sum(1 for t in titles if t.startswith("oversized")) == 1
    assert not any(t.startswith("unbounded") for t in titles)
    db.reset_engine()


def test_growth_suppressed_for_orphan_workspace(tmp_path, monkeypatch):
    """Growth inside an orphan workspace (no Ticket row) is suppressed —
    the orphan check files its own finding with the full dir size, so a
    growth ticket on the same path would be redundant noise."""
    s = _make_settings(tmp_path)
    monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
    board_id = "test-board"
    ticket_id = "20260101T000000Z-orph-cccc"
    _make_workspace_dir(s, board_id, ticket_id)
    db.init_db(s, board_id)  # board DB exists, but no ticket row

    run_data_dir_audit_pass()  # baseline
    _grow_workspace_file(s, board_id, ticket_id)

    result = run_data_dir_audit_pass()

    ws_prefix = f"workspaces/{ticket_id}"
    assert not any(f["path"].startswith(ws_prefix) for f in result.growth_flags)
    db.reset_engine()


# ---------------------------------------------------------------------------
# Terminal-clone GC — repo/ + repos/ inside terminal-ticket workspaces
# ---------------------------------------------------------------------------


def _make_workspace_with_clones(settings: Settings, board_id: str, ticket_id: str):
    """Workspace with description + artifacts + both clone subdirs."""
    ws_dir = _make_workspace_dir(settings, board_id, ticket_id)
    (ws_dir / "artifacts").mkdir(exist_ok=True)
    (ws_dir / "artifacts" / "retrospect.md").write_text("kept\n")
    _write_bytes(ws_dir / "repo" / ".git" / "objects.bin", 1_000)
    _write_bytes(ws_dir / "repos" / "other-repo" / ".git" / "objects.bin", 1_000)
    return ws_dir


def test_prune_terminal_clones_removes_clones_keeps_artifacts(tmp_path, monkeypatch):
    """The default-on clone GC removes repo/ + repos/ of an old terminal
    ticket but preserves description.md and artifacts/."""
    s = _make_settings(tmp_path)
    monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
    board_id = "test-board"
    ticket_id = "20260101T000000Z-closed-eeee"
    ws_dir = _make_workspace_with_clones(s, board_id, ticket_id)
    _insert_closed_ticket(
        s,
        board_id,
        ticket_id,
        closed_at=_now() - timedelta(days=30),
        state=State.CLOSED,
    )

    result = run_data_dir_audit_pass()

    assert result.clones_pruned == 2
    assert not (ws_dir / "repo").exists()
    assert not (ws_dir / "repos").exists()
    assert (ws_dir / "description.md").exists()
    assert (ws_dir / "artifacts" / "retrospect.md").exists()
    assert "Terminal-ticket clones pruned: 2." in result.summary
    db.reset_engine()


def test_prune_terminal_clones_age_guard_and_active_kept(tmp_path, monkeypatch):
    """Recently-closed and active tickets keep their clones."""
    s = _make_settings(tmp_path)
    monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
    board_id = "test-board"
    recent = "20260101T000000Z-recent-ffff"
    active = "20260101T000000Z-active-0000"
    ws_recent = _make_workspace_with_clones(s, board_id, recent)
    ws_active = _make_workspace_with_clones(s, board_id, active)
    _insert_closed_ticket(s, board_id, recent, closed_at=_now(), state=State.CLOSED)
    _insert_ticket(s, board_id, active)

    result = run_data_dir_audit_pass()

    assert result.clones_pruned == 0
    assert (ws_recent / "repo").exists()
    assert (ws_active / "repo").exists()
    db.reset_engine()


def test_prune_terminal_clones_knob_off_is_noop(tmp_path, monkeypatch):
    """With the knob disabled, no clones are touched."""
    s = _make_settings(tmp_path, data_dir_audit_prune_terminal_clones=False)
    monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
    board_id = "test-board"
    ticket_id = "20260101T000000Z-closed-1111"
    ws_dir = _make_workspace_with_clones(s, board_id, ticket_id)
    _insert_closed_ticket(
        s,
        board_id,
        ticket_id,
        closed_at=_now() - timedelta(days=30),
        state=State.CLOSED,
    )

    result = run_data_dir_audit_pass()

    assert result.clones_pruned == 0
    assert (ws_dir / "repo").exists()
    db.reset_engine()


# ---------------------------------------------------------------------------
# Growth breakdown + explained-aggregate suppression
# ---------------------------------------------------------------------------


def test_aggregate_workspaces_growth_suppressed_when_explained(
    tmp_path, monkeypatch, caplog
):
    """The aggregate ``workspaces/`` dir flag is suppressed when its
    growth is fully attributable to (individually suppressed) active
    ticket workspaces — the exact live failure of ticket 0ea7."""
    s = _make_settings(tmp_path)
    monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
    board_id = "test-board"
    ticket_id = "20260101T000000Z-active-2222"
    _make_workspace_dir(s, board_id, ticket_id)
    _insert_ticket(s, board_id, ticket_id)

    run_data_dir_audit_pass()  # baseline
    _grow_workspace_file(s, board_id, ticket_id)

    with caplog.at_level(logging.INFO, logger="robotsix_mill.data_dir_audit"):
        result = run_data_dir_audit_pass()

    # Neither the per-ticket path NOR the aggregate workspaces/ dir flags.
    assert not any(f["path"].startswith("workspaces/") for f in result.growth_flags)
    assert any("self-healing workspace churn" in rec.message for rec in caplog.records)
    db.reset_engine()


def test_unexplained_growth_keeps_flag_with_breakdown(tmp_path, monkeypatch):
    """Growth outside any workspace taxonomy survives suppression and
    carries a classified breakdown for the filed ticket."""
    s = _make_settings(tmp_path)
    monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
    board_id = "test-board"
    db.init_db(s, board_id)
    _write_bytes(s.data_dir / board_id / "logs" / "app.log", 100)

    run_data_dir_audit_pass()  # baseline
    _write_bytes(s.data_dir / board_id / "logs" / "app.log", 20_000_000)

    result = run_data_dir_audit_pass()

    dir_flags = [f for f in result.growth_flags if f["path"] == "logs/"]
    assert dir_flags, result.growth_flags
    flag = dir_flags[0]
    assert flag["explained_pct"] == 0.0
    assert flag["breakdown"]
    assert flag["breakdown"][0]["path"] == "logs/app.log"
    assert flag["breakdown"][0]["classification"] == "other"
    db.reset_engine()


def test_build_growth_finding_renders_breakdown_table():
    """The filed ticket body contains the breakdown table and the
    self-diagnosis guidance instead of a bare 'investigate' ask."""
    from robotsix_mill.runners.data_dir_audit_runner import _build_growth_finding

    flag = {
        "check": "growth_delta",
        "path": "logs/",
        "board_id": "test-board",
        "current_size_bytes": 21_000_000,
        "prior_size_bytes": 1_000_000,
        "delta_bytes": 20_000_000,
        "delta_pct": 2000.0,
        "threshold_exceeded": "both",
        "explained_pct": 12.5,
        "breakdown": [
            {
                "path": "logs/app.log",
                "delta_bytes": 17_500_000,
                "classification": "other",
            },
            {
                "path": "logs/cache/",
                "delta_bytes": 2_500_000,
                "classification": "periodic-pass clone (re-cloned every pass)",
            },
        ],
    }
    gap_id, title, body = _build_growth_finding(flag)

    assert gap_id == "growth:test-board:logs/"
    assert "## Growth breakdown (top contributors)" in body
    assert "`logs/app.log`" in body
    assert "| other |" in body
    assert "~12.5% of this growth is attributable" in body
    assert "Investigate why this path grew" not in body
    assert "spec a code fix" in body


def test_build_growth_finding_without_breakdown_still_guides():
    """A flag with no breakdown (file path, no children) renders the
    guidance footer without a breakdown section."""
    from robotsix_mill.runners.data_dir_audit_runner import _build_growth_finding

    flag = {
        "check": "growth_delta",
        "path": "big.log",
        "board_id": "test-board",
        "current_size_bytes": 21_000_000,
        "prior_size_bytes": 1_000_000,
        "delta_bytes": 20_000_000,
        "delta_pct": 2000.0,
        "threshold_exceeded": "both",
    }
    _gap_id, _title, body = _build_growth_finding(flag)
    assert "## Growth breakdown" not in body
    assert "spec a code fix" in body


# ---------------------------------------------------------------------------
# Oversized self-healing suppression (clone cache / periodic pass)
# ---------------------------------------------------------------------------


class TestOversizedSelfHealingSuppression:
    """Tests for ``_path_is_self_healing`` and
    ``_self_healing_oversized_paths`` — the oversized-check equivalents
    of the growth-check's self-healing classification."""

    def test_meta_clone_cache_files_suppressed(self, tmp_path, monkeypatch, caplog):
        """Files under ``meta/workspace/...`` are suppressed from oversized."""
        s = _make_settings(tmp_path)
        monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
        db.init_db(s, "meta")

        # Create an oversized clone-cache file inside the meta board.
        _make_sparse_file(
            s.data_dir
            / "meta"
            / "workspace"
            / "some-repo"
            / ".git"
            / "objects"
            / "ab"
            / "foo",
            200 * 1024 * 1024,
        )

        with caplog.at_level(logging.INFO, logger="robotsix_mill.data_dir_audit"):
            result = run_data_dir_audit_pass()

        # The clone file should be suppressed.
        clone_paths = [
            r["path"] for r in result.oversized_items if "workspace" in r["path"]
        ]
        assert clone_paths == [], f"clone paths not suppressed: {clone_paths}"
        assert any(
            "suppressing oversized item" in rec.message
            and "self-healing clone cache" in rec.message
            for rec in caplog.records
        )
        db.reset_engine()

    def test_meta_aggregate_dir_suppressed_when_mostly_self_healing(
        self, tmp_path, monkeypatch, caplog
    ):
        """``meta`` and ``meta/workspace`` dirs are suppressed when >=90%
        of their bytes are self-healing clone-cache files."""
        s = _make_settings(tmp_path)
        monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
        db.init_db(s, "meta")

        # 950 MB of clone-cache + 100 MB of genuine data = 1050 MB total
        # → 90.5% self-healing → dirs suppressed (just above 90%).
        _make_sparse_file(
            s.data_dir / "meta" / "workspace" / "r" / ".git" / "objects.bin",
            950 * 1024 * 1024,
        )
        _make_sparse_file(
            s.data_dir / "meta" / "genuine.log",
            100 * 1024 * 1024,
        )

        with caplog.at_level(logging.INFO, logger="robotsix_mill.data_dir_audit"):
            result = run_data_dir_audit_pass()

        suppressed_paths = {r["path"] for r in result.oversized_items}
        # The genuine file IS oversized and should still appear.
        assert "meta/genuine.log" in suppressed_paths
        # The aggregate dirs should NOT appear (suppressed).
        assert "meta" not in suppressed_paths
        assert "meta/workspace" not in suppressed_paths
        db.reset_engine()

    def test_board_root_suppressed_but_genuine_child_surfaces(
        self, tmp_path, monkeypatch
    ):
        """A board root is an aggregate rollup — always suppressed (never an
        independently actionable "oversized <board>" alert) — yet the genuine
        oversized FILE beneath it still surfaces on its own."""
        s = _make_settings(tmp_path)
        monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
        db.init_db(s, "meta")

        _make_sparse_file(
            s.data_dir / "meta" / "workspace" / "r" / ".git" / "objects.bin",
            80 * 1024 * 1024,
        )
        _make_sparse_file(
            s.data_dir / "meta" / "big_genuine.log",
            110 * 1024 * 1024,
        )

        result = run_data_dir_audit_pass()

        paths = {r["path"] for r in result.oversized_items}
        # The aggregate meta board root is suppressed (rollup, not actionable).
        assert "meta" not in paths, f"board root should be suppressed: {paths}"
        # The genuine file still surfaces individually.
        assert "meta/big_genuine.log" in paths
        db.reset_engine()

    def test_periodic_pass_clone_suppressed(self, tmp_path, monkeypatch, caplog):
        """Periodic-pass clone caches (e.g. ``health_workspace/repo/...``)
        are suppressed from oversized, regardless of board."""
        s = _make_settings(tmp_path)
        monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
        board_id = "test-board"
        db.init_db(s, board_id)

        _make_sparse_file(
            s.data_dir
            / board_id
            / "health_workspace"
            / "repo"
            / ".git"
            / "objects.bin",
            250 * 1024 * 1024,
        )

        with caplog.at_level(logging.INFO, logger="robotsix_mill.data_dir_audit"):
            result = run_data_dir_audit_pass()

        clone_paths = [
            r["path"] for r in result.oversized_items if "health_workspace" in r["path"]
        ]
        assert clone_paths == [], f"periodic-pass paths not suppressed: {clone_paths}"
        assert any(
            "suppressing oversized item" in rec.message
            and "self-healing clone cache" in rec.message
            for rec in caplog.records
        )
        db.reset_engine()

    def test_genuine_oversized_file_not_suppressed(self, tmp_path, monkeypatch):
        """A genuinely-oversized non-clone file (``mill.db``, ``big.log``,
        ``*_memory.md``) remains reportable."""
        s = _make_settings(tmp_path)
        monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
        db.init_db(s, "test-board")

        # A large mill.db is genuine — NOT a clone cache.
        _make_sparse_file(
            s.data_dir / "test-board" / "mill.db",
            150 * 1024 * 1024,
        )

        result = run_data_dir_audit_pass()

        paths = {r["path"] for r in result.oversized_items}
        assert "test-board/mill.db" in paths
        db.reset_engine()

    def test_top_n_preserved_after_suppression(self, tmp_path, monkeypatch):
        """When ≥10 self-healing items crowd out genuine oversized items,
        the genuine items STILL appear in the top-10 result (suppression
        happens BEFORE the ``[:top_n]`` slice)."""
        s = _make_settings(tmp_path)
        monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
        db.init_db(s, "meta")

        # Create 12 clone-cache files (all > threshold) + 3 genuine files.
        for i in range(12):
            _make_sparse_file(
                s.data_dir
                / "meta"
                / "workspace"
                / f"repo_{i}"
                / ".git"
                / "objects.bin",
                200 * 1024 * 1024,
            )
        for i in range(3):
            _make_sparse_file(
                s.data_dir / "meta" / f"genuine_{i}.log",
                300 * 1024 * 1024,
            )

        result = run_data_dir_audit_pass()

        # All clone-cache files should be suppressed.
        clone_paths = [
            r["path"] for r in result.oversized_items if "workspace" in r["path"]
        ]
        assert clone_paths == []

        # All 3 genuine files should appear (well within top-10).
        genuine = [r for r in result.oversized_items if "genuine" in r["path"]]
        assert len(genuine) == 3

        # The total returned is at most 10.
        assert len(result.oversized_items) <= 10
        db.reset_engine()

    def test_non_board_paths_not_suppressed(self, tmp_path):
        """Paths whose first segment is not a known board id are never
        classified as self-healing (they stay reportable)."""
        s = _make_settings(tmp_path)
        # No board DBs at all.
        _make_sparse_file(s.data_dir / "big.log", 200 * 1024 * 1024)

        result = find_largest_items(s.data_dir, threshold_bytes=100 * 1024 * 1024)

        assert len(result) >= 1
        assert result[0]["path"] == "big.log"

    def test_mixed_board_genuine_and_clone_cache(self, tmp_path, monkeypatch):
        """A board with both clone-cache and genuine oversize: genuine
        surfaces, clone-cache is suppressed."""
        s = _make_settings(tmp_path)
        monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
        board_id = "meta"
        db.init_db(s, board_id)

        # Clone cache: 200 MB
        _make_sparse_file(
            s.data_dir / board_id / "workspace" / "r" / ".git" / "objects.bin",
            200 * 1024 * 1024,
        )
        # Genuine oversized: 150 MB
        _make_sparse_file(
            s.data_dir / board_id / "big.log",
            150 * 1024 * 1024,
        )

        result = run_data_dir_audit_pass()

        paths = {r["path"] for r in result.oversized_items}
        # Clone-cache paths suppressed.
        clone = [p for p in paths if "workspace" in p]
        assert clone == [], f"clone paths leaked: {clone}"
        # Genuine file present.
        assert f"{board_id}/big.log" in paths
        # The aggregate ``meta`` board root is a rollup → always suppressed.
        assert board_id not in paths
        db.reset_engine()

    # ------------------------------------------------------------------
    # Terminal-ticket clone suppression (oversized check)
    # ------------------------------------------------------------------

    def test_terminal_clone_file_suppressed(self, tmp_path, monkeypatch):
        """A 200 MiB sparse file inside a terminal ticket's ``repo/`` dir
        is suppressed from ``result.oversized_items`` when the GC knob
        is on (default)."""
        s = _make_settings(tmp_path)
        monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
        board_id = "test-board"
        ticket_id = "20260101T000000Z-closed-ffff"
        _make_workspace_with_clones(s, board_id, ticket_id)
        _insert_closed_ticket(
            s,
            board_id,
            ticket_id,
            closed_at=_now() - timedelta(hours=12),
            state=State.CLOSED,
        )
        _make_sparse_file(
            s.data_dir
            / board_id
            / "workspaces"
            / ticket_id
            / "repo"
            / ".git"
            / "objects"
            / "big.bin",
            200 * 1024 * 1024,
        )

        result = run_data_dir_audit_pass()

        clone_paths = [
            r["path"] for r in result.oversized_items if ticket_id in r["path"]
        ]
        assert clone_paths == [], f"terminal clone paths not suppressed: {clone_paths}"
        db.reset_engine()

    def test_terminal_clone_aggregate_dir_suppressed(
        self, tmp_path, monkeypatch, caplog
    ):
        """A ``workspaces/`` dir whose bytes are ≥90% terminal-ticket
        clones is suppressed; a genuine oversized file in the same
        board still surfaces."""
        s = _make_settings(tmp_path)
        monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
        board_id = "test-board"
        ticket_id = "20260101T000000Z-closed-gggg"
        _make_workspace_with_clones(s, board_id, ticket_id)
        _insert_closed_ticket(
            s,
            board_id,
            ticket_id,
            closed_at=_now() - timedelta(hours=12),
            state=State.CLOSED,
        )
        # 950 MB clone → 90.5% of workspaces/ total (950+100=1050)
        _make_sparse_file(
            s.data_dir
            / board_id
            / "workspaces"
            / ticket_id
            / "repo"
            / ".git"
            / "objects.bin",
            950 * 1024 * 1024,
        )
        # 100 MB genuine file (not in workspaces/)
        _make_sparse_file(
            s.data_dir / board_id / "genuine.log",
            100 * 1024 * 1024,
        )

        with caplog.at_level(logging.INFO, logger="robotsix_mill.data_dir_audit"):
            result = run_data_dir_audit_pass()

        suppressed = {r["path"] for r in result.oversized_items}
        # Genuine file still appears.
        assert f"{board_id}/genuine.log" in suppressed
        # The workspaces/ aggregate dir should be suppressed (≥ 90%).
        assert f"{board_id}/workspaces" not in suppressed
        # The terminal-clone file itself suppressed.
        assert not any(ticket_id in p for p in suppressed)
        db.reset_engine()

    def test_workspace_clone_suppressed_even_when_gc_disabled(
        self, tmp_path, monkeypatch
    ):
        """Workspace-size suppression is unconditional: even with
        ``data_dir_audit_prune_terminal_clones=False`` (GC deletion off), a
        ticket-workspace clone is never reported as oversized — workspace
        size is transient infra, not an actionable alert. The GC knob governs
        deletion, not oversized reporting."""
        s = _make_settings(tmp_path, data_dir_audit_prune_terminal_clones=False)
        monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
        board_id = "test-board"
        ticket_id = "20260101T000000Z-closed-hhhh"
        _make_workspace_with_clones(s, board_id, ticket_id)
        _insert_closed_ticket(
            s,
            board_id,
            ticket_id,
            closed_at=_now() - timedelta(hours=12),
            state=State.CLOSED,
        )
        _make_sparse_file(
            s.data_dir
            / board_id
            / "workspaces"
            / ticket_id
            / "repo"
            / ".git"
            / "objects"
            / "big.bin",
            200 * 1024 * 1024,
        )

        result = run_data_dir_audit_pass()

        clone_paths = [
            r["path"] for r in result.oversized_items if ticket_id in r["path"]
        ]
        assert clone_paths == [], (
            f"workspace clone should be suppressed even with GC off, got: {result.oversized_items}"
        )
        db.reset_engine()

    def test_active_ticket_workspace_suppressed(self, tmp_path, monkeypatch):
        """Files inside an *active* (DRAFT) ticket workspace are suppressed
        from the oversized check — a live ticket's workspace is legitimately
        large and is never an actionable oversized alert."""
        s = _make_settings(tmp_path)
        monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
        board_id = "test-board"
        ticket_id = "20260101T000000Z-draft-iiii"
        _make_workspace_with_clones(s, board_id, ticket_id)
        _insert_ticket(s, board_id, ticket_id)  # DRAFT → active
        _make_sparse_file(
            s.data_dir
            / board_id
            / "workspaces"
            / ticket_id
            / "repo"
            / ".git"
            / "objects"
            / "big.bin",
            200 * 1024 * 1024,
        )

        result = run_data_dir_audit_pass()

        clone_paths = [
            r["path"] for r in result.oversized_items if ticket_id in r["path"]
        ]
        assert clone_paths == [], (
            f"active-ticket workspace should be suppressed, got: {result.oversized_items}"
        )
        db.reset_engine()

    def test_terminal_clone_suppression_logged(self, tmp_path, monkeypatch, caplog):
        """Suppression is logged at INFO level with
        ``"terminal-ticket clone cache"`` in the message."""
        s = _make_settings(tmp_path)
        monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
        board_id = "test-board"
        ticket_id = "20260101T000000Z-closed-jjjj"
        _make_workspace_with_clones(s, board_id, ticket_id)
        _insert_closed_ticket(
            s,
            board_id,
            ticket_id,
            closed_at=_now() - timedelta(hours=12),
            state=State.CLOSED,
        )
        _make_sparse_file(
            s.data_dir
            / board_id
            / "workspaces"
            / ticket_id
            / "repo"
            / ".git"
            / "objects"
            / "big.bin",
            200 * 1024 * 1024,
        )

        with caplog.at_level(logging.INFO, logger="robotsix_mill.data_dir_audit"):
            run_data_dir_audit_pass()

        assert any(
            "suppressing oversized item" in rec.message
            and "terminal-ticket clone cache" in rec.message
            for rec in caplog.records
        ), (
            f"no terminal-clone suppression log found in: {[r.message for r in caplog.records]}"
        )
        db.reset_engine()

    def test_mixed_terminal_clones_and_genuine_oversized(self, tmp_path, monkeypatch):
        """A board with both terminal-clone bloat (suppressed) and a
        genuinely oversized ``mill.db`` (reported) surfaces only the
        genuine file."""
        s = _make_settings(tmp_path)
        monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
        board_id = "test-board"
        ticket_id = "20260101T000000Z-closed-kkkk"
        _make_workspace_with_clones(s, board_id, ticket_id)
        _insert_closed_ticket(
            s,
            board_id,
            ticket_id,
            closed_at=_now() - timedelta(hours=12),
            state=State.CLOSED,
        )
        # Terminal-clone bloat: 200 MB
        _make_sparse_file(
            s.data_dir
            / board_id
            / "workspaces"
            / ticket_id
            / "repo"
            / ".git"
            / "objects"
            / "big.bin",
            200 * 1024 * 1024,
        )
        # Genuine oversized: 150 MB
        _make_sparse_file(
            s.data_dir / board_id / "mill.db",
            150 * 1024 * 1024,
        )

        result = run_data_dir_audit_pass()

        paths = {r["path"] for r in result.oversized_items}
        # Terminal-clone paths suppressed.
        clone = [p for p in paths if ticket_id in p]
        assert clone == [], f"terminal clone paths leaked: {clone}"
        # Genuine oversized file present.
        assert f"{board_id}/mill.db" in paths
        db.reset_engine()


def test_oversized_noise_scenario_files_nothing(tmp_path, monkeypatch):
    """End-to-end: the real-world noise pattern — a board whose disk is
    dominated by a large active-ticket workspace — produces ZERO oversized
    items (no "oversized <board>", "<board>/workspaces", or per-ticket
    workspace alerts). This is the case that generated the recurring blocked
    "oversized" tickets."""
    s = _make_settings(tmp_path, data_dir_audit_size_threshold_bytes=1_000_000)
    monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
    board_id = "robotsix-mill"
    ticket_id = "20260101T000000Z-active-wxyz"
    db.init_db(s, board_id)
    _make_workspace_dir(s, board_id, ticket_id)
    _insert_ticket(s, board_id, ticket_id)  # active (DRAFT)
    # Big clone + venv inside the live workspace (the inherent, transient bulk).
    _make_sparse_file(
        s.data_dir / board_id / "workspaces" / ticket_id / "repo" / ".git" / "big.bin",
        600 * 1024 * 1024,
    )

    result = run_data_dir_audit_pass()

    paths = {r["path"] for r in result.oversized_items}
    assert paths == set(), f"expected no oversized findings, got: {paths}"
    db.reset_engine()


# ---------------------------------------------------------------------------
#  Memory-ledger GC step (ticket: unbounded-robotsix-auto-mail-retrospect)
# ---------------------------------------------------------------------------


class TestPruneOversizedMemoryLedgers:
    """Tests for ``_prune_oversized_memory_ledgers`` and its integration."""

    def test_knob_on_truncates_over_cap_file(self, tmp_path, monkeypatch):
        """With the knob on (default), an over-cap memory ledger is
        truncated on disk before size measurement, and no unbounded
        finding is produced."""
        s = _make_settings(tmp_path, max_memory_chars=100)
        ledger = tmp_path / "retrospect_memory.md"
        _write_bytes(ledger, 5000)

        monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
        result = run_data_dir_audit_pass()

        # File was truncated on disk.
        assert ledger.stat().st_size <= 100
        # GC count reflected.
        assert result.memory_ledgers_truncated >= 1
        # No unbounded finding (file now under cap).
        memory_findings = [
            f for f in result.findings if f.get("pattern") == "*_memory.md"
        ]
        assert memory_findings == []

    def test_knob_off_leaves_file_untouched(self, tmp_path, monkeypatch):
        """With the knob off, the over-cap file is left untouched and
        the unbounded finding is still produced."""
        s = _make_settings(
            tmp_path,
            max_memory_chars=100,
            data_dir_audit_prune_memory_ledgers=False,
        )
        ledger = tmp_path / "retrospect_memory.md"
        _write_bytes(ledger, 5000)

        monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
        result = run_data_dir_audit_pass()

        # File NOT truncated.
        assert ledger.stat().st_size >= 5000
        # GC count stays zero.
        assert result.memory_ledgers_truncated == 0
        # Unbounded finding still produced.
        memory_findings = [
            f for f in result.findings if f.get("pattern") == "*_memory.md"
        ]
        assert len(memory_findings) == 1

    def test_file_at_or_under_cap_left_byte_identical(self, tmp_path, monkeypatch):
        """A memory ledger at or under the cap is left untouched."""
        s = _make_settings(tmp_path, max_memory_chars=5000)
        ledger = tmp_path / "small_memory.md"
        original = "## Memory\n\n- line 1\n- line 2\n"
        ledger.write_text(original, encoding="utf-8")

        monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
        result = run_data_dir_audit_pass()

        assert result.memory_ledgers_truncated == 0
        assert ledger.read_text(encoding="utf-8") == original

    def test_guard_max_memory_chars_zero(self, tmp_path, monkeypatch):
        """When max_memory_chars <= 0, no file is touched (guard against
        truncating to empty)."""
        s = _make_settings(tmp_path, max_memory_chars=0)
        ledger = tmp_path / "some_memory.md"
        _write_bytes(ledger, 500)

        monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
        result = run_data_dir_audit_pass()

        assert result.memory_ledgers_truncated == 0
        assert ledger.stat().st_size >= 500

    def test_guard_missing_data_dir(self, tmp_path, monkeypatch):
        """When data_dir does not exist, the helper returns 0."""
        s = _make_settings(tmp_path, max_memory_chars=100)
        # data_dir is tmp_path — but we'll make it point to a nonexistent
        # subdirectory.
        s.data_dir = tmp_path / "nonexistent"

        monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
        result = run_data_dir_audit_pass()

        assert result.memory_ledgers_truncated == 0

    def test_nested_ledgers_in_board_subdirs(self, tmp_path, monkeypatch):
        """Memory ledgers nested inside board subdirectories are found
        and truncated."""
        s = _make_settings(tmp_path, max_memory_chars=100)
        board_dir = tmp_path / "robotsix-auto-mail"
        board_dir.mkdir()
        ledger = board_dir / "retrospect_memory.md"
        _write_bytes(ledger, 5000)

        monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
        result = run_data_dir_audit_pass()

        assert result.memory_ledgers_truncated >= 1
        assert ledger.stat().st_size <= 100

    def test_oserror_on_read_skipped(self, tmp_path, monkeypatch, caplog):
        """A file that raises OSError on read is skipped (not fatal)."""
        import errno

        s = _make_settings(tmp_path, max_memory_chars=100)
        ledger = tmp_path / "broken_memory.md"
        _write_bytes(ledger, 5000)

        # Patch open to fail for this specific path.
        original_open = Path.read_text

        def _failing_read_text(self, *args, **kwargs):
            if self == ledger:
                raise OSError(errno.EACCES, "Permission denied")
            return original_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _failing_read_text)
        monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)

        with caplog.at_level(logging.WARNING, logger="robotsix_mill.data_dir_audit"):
            result = run_data_dir_audit_pass()

        assert result.memory_ledgers_truncated == 0
        assert any("cannot read memory ledger" in rec.message for rec in caplog.records)

    def test_data_dir_is_file_not_directory(self, tmp_path, monkeypatch):
        """When data_dir is a file (not a directory), the helper
        returns 0 without crashing."""
        s = _make_settings(tmp_path, max_memory_chars=100)
        not_a_dir = tmp_path / "not_a_dir"
        not_a_dir.write_text("i am a file", encoding="utf-8")
        s.data_dir = not_a_dir

        monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
        result = run_data_dir_audit_pass()

        assert result.memory_ledgers_truncated == 0

    def test_summary_line_when_truncated(self, tmp_path, monkeypatch):
        """The summary includes a 'Memory ledgers truncated: N.' line."""
        s = _make_settings(tmp_path, max_memory_chars=100)
        _write_bytes(tmp_path / "audit_memory.md", 5000)

        monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
        result = run_data_dir_audit_pass()

        assert "Memory ledgers truncated: 1." in result.summary

    def test_summary_line_absent_when_none_truncated(self, tmp_path, monkeypatch):
        """When no files are truncated, the summary omits the line."""
        s = _make_settings(tmp_path, max_memory_chars=5000)
        ledger = tmp_path / "small_memory.md"
        ledger.write_text("small content", encoding="utf-8")

        monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
        result = run_data_dir_audit_pass()

        assert "Memory ledgers truncated:" not in result.summary

    def test_multiple_ledgers_all_truncated(self, tmp_path, monkeypatch):
        """Multiple over-cap ledgers are all truncated and counted."""
        s = _make_settings(tmp_path, max_memory_chars=100)
        _write_bytes(tmp_path / "foo_memory.md", 5000)
        _write_bytes(tmp_path / "bar_memory.md", 5000)

        monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
        result = run_data_dir_audit_pass()

        assert result.memory_ledgers_truncated == 2
        # Both files under cap.
        assert (tmp_path / "foo_memory.md").stat().st_size <= 100
        assert (tmp_path / "bar_memory.md").stat().st_size <= 100

    def test_byte_size_over_cap_char_count_under(self, tmp_path, monkeypatch):
        """A file whose UTF-8 byte size exceeds max_memory_chars but
        whose character count does not (e.g. due to multi-byte chars)
        is still truncated — the guard uses encoded byte size to match
        the unbounded-candidate check which compares st_size."""
        s = _make_settings(tmp_path, max_memory_chars=100)
        ledger = tmp_path / "copy_paste_memory.md"
        # 90 single-byte chars + 10 multi-byte (2-byte) chars:
        # char count = 100, byte count = 90 + 20 = 110 > 100
        content = ("x" * 90) + ("\N{LATIN SMALL LETTER E WITH ACUTE}" * 10)
        assert len(content) == 100
        assert len(content.encode("utf-8")) == 110
        ledger.write_text(content, encoding="utf-8")

        monkeypatch.setattr("robotsix_mill.runners.data_dir_audit.Settings", lambda: s)
        result = run_data_dir_audit_pass()

        # File was truncated.
        assert result.memory_ledgers_truncated >= 1
        assert ledger.stat().st_size <= 100
        # No unbounded finding.
        memory_findings = [
            f for f in result.findings if f.get("pattern") == "*_memory.md"
        ]
        assert memory_findings == []
