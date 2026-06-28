"""Tests for the orphaned-PR check runner."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from robotsix_mill.config import RepoConfig, Settings
from robotsix_mill.core.models import SourceKind, Ticket
from robotsix_mill.core.states import State
from robotsix_mill.runners.orphaned_pr_check import (
    _pr_has_empty_diff,
    _build_close_comment,
    run_orphaned_pr_check_pass,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(**overrides):
    overrides.setdefault("orphaned_pr_dry_run", False)
    overrides.setdefault("orphaned_pr_min_age_hours", 4)
    overrides.setdefault("orphaned_pr_max_actions_per_pass", 5)
    overrides.setdefault("branch_prefix", "mill/")
    # Provide a data_dir so TicketService can instantiate its db.
    overrides.setdefault("data_dir", "/tmp/orphaned_pr_test")
    return Settings(**overrides)


def _repo():
    return RepoConfig(
        repo_id="test-owner/test-repo",
        board_id="test-repo",
        langfuse_project_name="proj-test",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
    )


def _ticket(ticket_id: str = "20250101T000000Z-test-ticket-a1b2", **kw):
    kw.setdefault("state", State.READY)
    kw.setdefault("created_at", datetime(2025, 1, 10, tzinfo=timezone.utc))
    kw.setdefault("id", ticket_id)
    return Ticket(**kw)


_PR_STATUS_NOT_SET = object()


def _mock_forge(*, open_branches=None, pr_status=_PR_STATUS_NOT_SET, pr_files=None):
    """Build a mock forge object with configurable return values."""
    forge = MagicMock()
    forge.list_open_pr_branches.return_value = open_branches or set()
    if pr_status is _PR_STATUS_NOT_SET:
        forge.pr_status.return_value = {"state": "open"}
    else:
        forge.pr_status.return_value = pr_status
    forge.pr_files.return_value = (
        pr_files
        if pr_files is not None
        else [{"path": "a.py", "additions": 5, "deletions": 3}]
    )
    return forge


def _install_seams(monkeypatch, settings, forge, service):
    """Wire Settings(), get_forge(), and TicketService seams."""
    monkeypatch.setattr(
        "robotsix_mill.runners.orphaned_pr_check.Settings",
        lambda: settings,
    )
    monkeypatch.setattr(
        "robotsix_mill.runners.orphaned_pr_check.get_forge",
        lambda *a, **kw: forge,
    )
    monkeypatch.setattr(
        "robotsix_mill.runners.orphaned_pr_check.TicketService",
        lambda *a, **kw: service,
    )


# ---------------------------------------------------------------------------
# Unit tests — helpers
# ---------------------------------------------------------------------------


class TestPrHasEmptyDiff:
    def test_empty_list(self):
        forge = _mock_forge(pr_files=[])
        assert _pr_has_empty_diff(forge, "mill/abc") is True

    def test_all_zero_diffs(self):
        forge = _mock_forge(
            pr_files=[
                {"path": "a.py", "additions": 0, "deletions": 0},
                {"path": "b.py", "additions": 0, "deletions": 0},
            ]
        )
        assert _pr_has_empty_diff(forge, "mill/abc") is True

    def test_has_real_changes(self):
        forge = _mock_forge(
            pr_files=[
                {"path": "a.py", "additions": 0, "deletions": 0},
                {"path": "b.py", "additions": 5, "deletions": 0},
            ]
        )
        assert _pr_has_empty_diff(forge, "mill/abc") is False

    def test_missing_additions_deletions_keys(self):
        forge = _mock_forge(
            pr_files=[
                {"path": "a.py"},
            ]
        )
        assert _pr_has_empty_diff(forge, "mill/abc") is True


class TestBuildCloseComment:
    def test_with_ticket(self):
        t = _ticket("20250101T000000Z-test-ticket-a1b2", state=State.DONE)
        comment = _build_close_comment(
            "owner/repo", "mill/20250101T000000Z-test-ticket-a1b2", t
        )
        assert "20250101T000000Z-test-ticket-a1b2" in comment
        assert "done" in comment
        assert "orphaned-pr" in comment.lower()

    def test_without_ticket(self):
        comment = _build_close_comment("owner/repo", "mill/unknown", None)
        assert "empty diff" in comment.lower()


# ---------------------------------------------------------------------------
# Integration tests — run_orphaned_pr_check_pass
# ---------------------------------------------------------------------------


class TestActiveTicketNotOrphaned:
    def test_ticket_in_ready_state_skipped(self, monkeypatch):
        """Ticket in READY state → branch skipped, no action."""
        s = _settings()
        repo = _repo()
        ticket = _ticket("20250101T000000Z-test-ticket-a1b2", state=State.READY)
        svc = MagicMock()
        svc.get.return_value = ticket
        forge = _mock_forge(open_branches={"mill/20250101T000000Z-test-ticket-a1b2"})
        _install_seams(monkeypatch, s, forge, svc)

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert result.total_scanned == 1
        assert result.closed == 0
        assert result.filed == 0
        forge.close_pr.assert_not_called()
        forge.post_pr_comment.assert_not_called()


class TestTicketDoneOpenPrClose:
    def test_closes_pr_with_comment(self, monkeypatch):
        """Ticket DONE, PR open, dry_run=False → close_pr + comment."""
        s = _settings(orphaned_pr_dry_run=False)
        repo = _repo()
        ticket = _ticket("20250101T000000Z-test-ticket-a1b2", state=State.DONE)
        svc = MagicMock()
        svc.get.return_value = ticket
        forge = _mock_forge(
            open_branches={"mill/20250101T000000Z-test-ticket-a1b2"},
        )
        _install_seams(monkeypatch, s, forge, svc)

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert result.closed == 1
        assert result.filed == 0
        forge.post_pr_comment.assert_called_once()
        forge.close_pr.assert_called_once_with(
            source_branch="mill/20250101T000000Z-test-ticket-a1b2"
        )


class TestTicketClosedOpenPrClose:
    def test_closes_pr(self, monkeypatch):
        """Ticket CLOSED, PR open, dry_run=False → close_pr + comment."""
        s = _settings(orphaned_pr_dry_run=False)
        repo = _repo()
        ticket = _ticket("20250101T000000Z-test-ticket-a1b2", state=State.CLOSED)
        svc = MagicMock()
        svc.get.return_value = ticket
        forge = _mock_forge(
            open_branches={"mill/20250101T000000Z-test-ticket-a1b2"},
        )
        _install_seams(monkeypatch, s, forge, svc)

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert result.closed == 1
        forge.close_pr.assert_called_once()


class TestTicketErroredNonemptyDiffFile:
    def test_files_tracking_ticket(self, monkeypatch):
        """Ticket ERRORED, non-empty diff → file tracking ticket."""
        s = _settings(orphaned_pr_dry_run=False)
        repo = _repo()
        ticket = _ticket("20250101T000000Z-test-ticket-a1b2", state=State.ERRORED)
        svc = MagicMock()
        svc.get.return_value = ticket
        forge = _mock_forge(
            open_branches={"mill/20250101T000000Z-test-ticket-a1b2"},
            pr_files=[{"path": "x.py", "additions": 10, "deletions": 0}],
        )
        _install_seams(monkeypatch, s, forge, svc)

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert result.filed == 1
        assert result.closed == 0
        svc.create.assert_called_once()
        call_args = svc.create.call_args
        assert call_args.kwargs["title"] == (
            "Track orphaned PR: test-owner/test-repo/"
            "mill/20250101T000000Z-test-ticket-a1b2"
        )
        assert call_args.kwargs["source"] == SourceKind.ORPHANED_PR_CHECK


class TestNoTicketEmptyDiffClose:
    def test_closes_pr_no_ticket_empty_diff(self, monkeypatch):
        """service.get returns None, pr_files=[] → close_pr."""
        s = _settings(orphaned_pr_dry_run=False)
        repo = _repo()
        svc = MagicMock()
        svc.get.return_value = None
        forge = _mock_forge(
            open_branches={"mill/20250101T000000Z-orphan-a1b2"},
            pr_files=[],
        )
        _install_seams(monkeypatch, s, forge, svc)

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert result.closed == 1
        forge.close_pr.assert_called_once()


class TestNoTicketNonemptyDiffFile:
    def test_files_ticket_no_ticket_nonempty_diff(self, monkeypatch):
        """service.get returns None, pr_files has content → file."""
        s = _settings(orphaned_pr_dry_run=False)
        repo = _repo()
        svc = MagicMock()
        svc.get.return_value = None
        forge = _mock_forge(
            open_branches={"mill/20250101T000000Z-orphan-a1b2"},
        )
        _install_seams(monkeypatch, s, forge, svc)

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert result.filed == 1
        svc.create.assert_called_once()


class TestAgeGuardSkipsYoungTicket:
    def test_skips_young_ticket(self, monkeypatch):
        """Ticket created 1h ago, min_age_hours=4 → skipped."""
        s = _settings(
            orphaned_pr_dry_run=False,
            orphaned_pr_min_age_hours=4,
        )
        repo = _repo()
        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        ticket = _ticket(
            "20250101T000000Z-test-ticket-a1b2",
            state=State.DONE,
            created_at=recent,
        )
        svc = MagicMock()
        svc.get.return_value = ticket
        forge = _mock_forge(
            open_branches={"mill/20250101T000000Z-test-ticket-a1b2"},
        )
        _install_seams(monkeypatch, s, forge, svc)

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert result.skipped == 1
        assert result.closed == 0
        assert result.filed == 0
        forge.close_pr.assert_not_called()


class TestActionCapStopsEarly:
    def test_caps_at_max_actions(self, monkeypatch):
        """10 orphaned PRs, max_actions_per_pass=3 → exactly 3 actions."""
        s = _settings(
            orphaned_pr_dry_run=False,
            orphaned_pr_max_actions_per_pass=3,
        )
        repo = _repo()
        svc = MagicMock()
        svc.get.return_value = None  # all are orphans
        branches = {f"mill/ticket-{i:04d}" for i in range(10)}
        forge = _mock_forge(open_branches=branches)
        _install_seams(monkeypatch, s, forge, svc)

        result = run_orphaned_pr_check_pass(repo_config=repo)

        # All no-ticket orphans with non-empty diff → filed
        assert result.filed == 3
        assert result.total_scanned == 10
        # Check cap message in actions
        cap_lines = [a for a in result.actions if "action cap" in a]
        assert len(cap_lines) == 1
        assert "7 branch(es) remain unprocessed" in cap_lines[0]


class TestDryRunNoMutations:
    def test_dry_run_no_forge_calls(self, monkeypatch):
        """dry_run=True (default) → no forge mutations."""
        s = _settings(orphaned_pr_dry_run=True)
        repo = _repo()
        ticket = _ticket("20250101T000000Z-test-ticket-a1b2", state=State.DONE)
        svc = MagicMock()
        svc.get.return_value = ticket
        forge = _mock_forge(
            open_branches={"mill/20250101T000000Z-test-ticket-a1b2"},
        )
        _install_seams(monkeypatch, s, forge, svc)

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert result.skipped == 1
        assert result.dry_run is True
        forge.close_pr.assert_not_called()
        forge.post_pr_comment.assert_not_called()
        svc.create.assert_not_called()


class TestPrAlreadyClosedSkipped:
    def test_pr_already_closed_no_action(self, monkeypatch):
        """pr_status returns state='closed' → no action."""
        s = _settings(orphaned_pr_dry_run=False)
        repo = _repo()
        ticket = _ticket("20250101T000000Z-test-ticket-a1b2", state=State.DONE)
        svc = MagicMock()
        svc.get.return_value = ticket
        forge = _mock_forge(
            open_branches={"mill/20250101T000000Z-test-ticket-a1b2"},
            pr_status={"state": "closed"},
        )
        _install_seams(monkeypatch, s, forge, svc)

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert result.total_scanned == 1
        assert result.closed == 0
        assert result.filed == 0
        forge.close_pr.assert_not_called()


class TestHumanPrNotProcessed:
    def test_non_mill_prefix_ignored(self, monkeypatch):
        """Branch 'feature/foo' (no mill/ prefix) → ignored."""
        s = _settings(orphaned_pr_dry_run=False)
        repo = _repo()
        svc = MagicMock()
        forge = _mock_forge(
            open_branches={"feature/foo", "bugfix/bar"},
        )
        _install_seams(monkeypatch, s, forge, svc)

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert result.total_scanned == 0
        assert result.closed == 0
        svc.get.assert_not_called()


class TestIdempotentFileTicket:
    def test_create_called_with_same_title(self, monkeypatch):
        """Second call with same branch produces identical title."""
        s = _settings(orphaned_pr_dry_run=False)
        repo = _repo()
        svc = MagicMock()
        svc.get.return_value = None
        forge = _mock_forge(
            open_branches={"mill/20250101T000000Z-orphan-a1b2"},
        )
        _install_seams(monkeypatch, s, forge, svc)

        # First pass
        run_orphaned_pr_check_pass(repo_config=repo)
        # Second pass — same branch still open
        run_orphaned_pr_check_pass(repo_config=repo)

        assert svc.create.call_count == 2
        # Both calls use the same deterministic title
        title1 = svc.create.call_args_list[0].kwargs["title"]
        title2 = svc.create.call_args_list[1].kwargs["title"]
        assert title1 == title2
        assert (
            title1
            == "Track orphaned PR: test-owner/test-repo/mill/20250101T000000Z-orphan-a1b2"
        )


class TestPrStatusNoneSkipped:
    def test_pr_status_returns_none(self, monkeypatch):
        """pr_status returns None → PR not found, skipped."""
        s = _settings(orphaned_pr_dry_run=False)
        repo = _repo()
        ticket = _ticket("20250101T000000Z-test-ticket-a1b2", state=State.DONE)
        svc = MagicMock()
        svc.get.return_value = ticket
        forge = _mock_forge(
            open_branches={"mill/20250101T000000Z-test-ticket-a1b2"},
            pr_status=None,
        )
        _install_seams(monkeypatch, s, forge, svc)

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert result.closed == 0
        forge.close_pr.assert_not_called()


class TestRepoConfigNone:
    def test_raises_on_none_repo_config(self):
        with pytest.raises(ValueError, match="requires a repo_config"):
            run_orphaned_pr_check_pass(repo_config=None)
