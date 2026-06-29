"""Tests for the orphaned-PR check runner."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from robotsix_mill.config import RepoConfig, Settings
from robotsix_mill.core.models import Ticket
from robotsix_mill.core.states import State
from robotsix_mill.runners.orphaned_pr_check import (
    OrphanClassification,
    ClassifiedOrphanPr,
    classify_orphaned_prs,
    _pr_has_empty_diff,
    _pr_has_conflicts,
    _determine_classification,
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
    overrides.setdefault("orphaned_pr_bot_logins", [])  # auto-resolve mode
    overrides.setdefault("orphaned_pr_max_closes_per_pass", 10)
    overrides.setdefault("orphaned_pr_max_files_per_pass", 5)
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


def _mock_forge(
    *,
    open_branches=None,
    pr_status=_PR_STATUS_NOT_SET,
    pr_files=None,
    bot_login="mill-bot",
):
    """Build a mock forge object with configurable return values."""
    forge = MagicMock()
    branches = open_branches or set()
    forge.list_open_pr_branches.return_value = branches
    forge.list_open_prs.return_value = [
        {"branch": b, "author_login": bot_login} for b in branches
    ]
    forge.get_authenticated_user_login.return_value = bot_login
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
    service.recent_proposals_for.return_value = []


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


class TestPrHasConflicts:
    def test_mergeable_true(self):
        forge = _mock_forge(pr_status={"state": "open", "mergeable": True})
        assert _pr_has_conflicts(forge, "mill/abc") is False

    def test_conflicts(self):
        forge = _mock_forge(pr_status={"state": "open", "mergeable": False})
        assert _pr_has_conflicts(forge, "mill/abc") is True

    def test_unknown_treated_as_no_conflict(self):
        forge = _mock_forge(pr_status={"state": "open", "mergeable": None})
        assert _pr_has_conflicts(forge, "mill/abc") is False

    def test_no_pr_status(self):
        forge = _mock_forge(pr_status=None)
        assert _pr_has_conflicts(forge, "mill/abc") is False


class TestDetermineClassification:
    def test_no_ticket_empty_diff(self):
        c = _determine_classification(None, empty_diff=True, has_conflicts=False)
        assert c == OrphanClassification.NO_TICKET_EMPTY_DIFF

    def test_no_ticket_nonempty(self):
        c = _determine_classification(None, empty_diff=False, has_conflicts=False)
        assert c == OrphanClassification.NO_TICKET

    def test_no_ticket_conflicting(self):
        c = _determine_classification(None, empty_diff=False, has_conflicts=True)
        assert c == OrphanClassification.NO_TICKET_CONFLICTING

    def test_done_nonempty_no_conflict(self):
        t = _ticket(state=State.DONE)
        c = _determine_classification(t, empty_diff=False, has_conflicts=False)
        assert c == OrphanClassification.TICKET_DONE_UNMERGED

    def test_done_empty_diff(self):
        t = _ticket(state=State.DONE)
        c = _determine_classification(t, empty_diff=True, has_conflicts=False)
        assert c == OrphanClassification.SUPERSEDED

    def test_done_conflicting(self):
        t = _ticket(state=State.DONE)
        c = _determine_classification(t, empty_diff=False, has_conflicts=True)
        assert c == OrphanClassification.TICKET_DONE_CONFLICTING

    def test_closed_nonempty_no_conflict(self):
        t = _ticket(state=State.CLOSED)
        c = _determine_classification(t, empty_diff=False, has_conflicts=False)
        assert c == OrphanClassification.TICKET_CLOSED_UNMERGED

    def test_closed_conflicting(self):
        t = _ticket(state=State.CLOSED)
        c = _determine_classification(t, empty_diff=False, has_conflicts=True)
        assert c == OrphanClassification.TICKET_CLOSED_CONFLICTING

    def test_errored_nonempty_no_conflict(self):
        t = _ticket(state=State.ERRORED)
        c = _determine_classification(t, empty_diff=False, has_conflicts=False)
        assert c == OrphanClassification.TICKET_ERRORED

    def test_errored_empty_diff(self):
        t = _ticket(state=State.ERRORED)
        c = _determine_classification(t, empty_diff=True, has_conflicts=False)
        assert c == OrphanClassification.TICKET_ERRORED_EMPTY_DIFF

    def test_errored_conflicting(self):
        t = _ticket(state=State.ERRORED)
        c = _determine_classification(t, empty_diff=False, has_conflicts=True)
        assert c == OrphanClassification.TICKET_ERRORED_CONFLICTING


class TestBuildCloseComment:
    def test_with_ticket_done(self):
        cpr = ClassifiedOrphanPr(
            branch="mill/20250101T000000Z-test-ticket-a1b2",
            ticket_id="20250101T000000Z-test-ticket-a1b2",
            classification=OrphanClassification.TICKET_DONE_UNMERGED,
            ticket_state="done",
        )
        comment = _build_close_comment(cpr, "owner/repo")
        assert "20250101T000000Z-test-ticket-a1b2" in comment
        assert "done" in comment
        assert "orphaned-pr" in comment.lower()

    def test_without_ticket_empty_diff(self):
        cpr = ClassifiedOrphanPr(
            branch="mill/unknown",
            ticket_id="unknown",
            classification=OrphanClassification.NO_TICKET_EMPTY_DIFF,
            ticket_state=None,
        )
        comment = _build_close_comment(cpr, "owner/repo")
        assert "empty diff" in comment.lower()


# ---------------------------------------------------------------------------
# Unit tests — classify_orphaned_prs (core algorithm)
# ---------------------------------------------------------------------------


class TestClassifyOrphanedPrs:
    def test_known_linked_pr_excluded(self):
        """Active ticket (READY) → not classified as orphaned."""
        s = _settings()
        svc = MagicMock()
        svc.get.return_value = _ticket(
            "20250101T000000Z-test-ticket-a1b2", state=State.READY
        )
        forge = _mock_forge(pr_status={"state": "open", "mergeable": True})
        result = classify_orphaned_prs(
            ["mill/20250101T000000Z-test-ticket-a1b2"],
            settings=s,
            service=svc,
            forge=forge,
        )
        assert result == []

    def test_unlinked_pr_included(self):
        """No ticket → classified as orphaned."""
        s = _settings()
        svc = MagicMock()
        svc.get.return_value = None
        forge = _mock_forge(pr_status={"state": "open", "mergeable": True})
        result = classify_orphaned_prs(
            ["mill/20250101T000000Z-orphan-a1b2"],
            settings=s,
            service=svc,
            forge=forge,
        )
        assert len(result) == 1
        assert result[0].classification == OrphanClassification.NO_TICKET

    def test_multiple_branches_mixed(self):
        """Active + orphan → only orphan returned."""
        s = _settings()
        svc = MagicMock()

        def _get(tid):
            if tid == "20250101T000000Z-active-b1b2":
                return _ticket("20250101T000000Z-active-b1b2", state=State.READY)
            return None

        svc.get.side_effect = _get
        forge = _mock_forge(pr_status={"state": "open", "mergeable": True})
        result = classify_orphaned_prs(
            [
                "mill/20250101T000000Z-active-b1b2",
                "mill/20250101T000000Z-orphan-c1d2",
            ],
            settings=s,
            service=svc,
            forge=forge,
        )
        assert len(result) == 1
        assert result[0].ticket_id == "20250101T000000Z-orphan-c1d2"

    def test_no_orphan_scenario(self):
        """All tickets active → empty list."""
        s = _settings()
        svc = MagicMock()
        svc.get.return_value = _ticket(
            "20250101T000000Z-test-ticket-a1b2", state=State.READY
        )
        forge = _mock_forge(pr_status={"state": "open", "mergeable": True})
        result = classify_orphaned_prs(
            ["mill/20250101T000000Z-test-ticket-a1b2"],
            settings=s,
            service=svc,
            forge=forge,
        )
        assert result == []

    def test_done_ticket_classified_as_orphan(self):
        """DONE ticket → orphaned with TICKET_DONE_UNMERGED."""
        s = _settings()
        svc = MagicMock()
        svc.get.return_value = _ticket(
            "20250101T000000Z-test-ticket-a1b2", state=State.DONE
        )
        forge = _mock_forge(pr_status={"state": "open", "mergeable": True})
        result = classify_orphaned_prs(
            ["mill/20250101T000000Z-test-ticket-a1b2"],
            settings=s,
            service=svc,
            forge=forge,
        )
        assert len(result) == 1
        assert result[0].classification == OrphanClassification.TICKET_DONE_UNMERGED

    def test_conflicting_pr_flag(self):
        """Conflicting PR → classification includes conflict marker."""
        s = _settings()
        svc = MagicMock()
        svc.get.return_value = _ticket(
            "20250101T000000Z-test-ticket-a1b2", state=State.DONE
        )
        forge = _mock_forge(pr_status={"state": "open", "mergeable": False})
        result = classify_orphaned_prs(
            ["mill/20250101T000000Z-test-ticket-a1b2"],
            settings=s,
            service=svc,
            forge=forge,
        )
        assert len(result) == 1
        assert result[0].classification == OrphanClassification.TICKET_DONE_CONFLICTING

    def test_pr_already_closed_excluded(self):
        """PR state=closed → not classified."""
        s = _settings()
        svc = MagicMock()
        svc.get.return_value = _ticket(
            "20250101T000000Z-test-ticket-a1b2", state=State.DONE
        )
        forge = _mock_forge(pr_status={"state": "closed"})
        result = classify_orphaned_prs(
            ["mill/20250101T000000Z-test-ticket-a1b2"],
            settings=s,
            service=svc,
            forge=forge,
        )
        assert result == []

    def test_classification_includes_ticket_state(self):
        """ClassifiedOrphanPr carries the ticket_state string."""
        s = _settings()
        svc = MagicMock()
        svc.get.return_value = _ticket(
            "20250101T000000Z-test-ticket-a1b2", state=State.ERRORED
        )
        forge = _mock_forge(pr_status={"state": "open", "mergeable": True})
        result = classify_orphaned_prs(
            ["mill/20250101T000000Z-test-ticket-a1b2"],
            settings=s,
            service=svc,
            forge=forge,
        )
        assert len(result) == 1
        assert result[0].ticket_state == "errored"


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
        assert result.classifications == []
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
        assert len(result.classifications) == 1
        assert (
            result.classifications[0].classification
            == OrphanClassification.TICKET_DONE_UNMERGED
        )
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
        assert len(result.classifications) == 1
        assert (
            result.classifications[0].classification
            == OrphanClassification.TICKET_ERRORED
        )
        svc.create.assert_called_once()


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
        assert len(result.classifications) == 1
        assert (
            result.classifications[0].classification
            == OrphanClassification.NO_TICKET_EMPTY_DIFF
        )
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
        assert len(result.classifications) == 1
        assert (
            result.classifications[0].classification == OrphanClassification.NO_TICKET
        )
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
        assert result.classifications == []
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
        assert len(result.classifications) == 10
        cap_lines = [a for a in result.actions if "action cap" in a]
        assert len(cap_lines) == 1
        assert "7 classification(s) remain unprocessed" in cap_lines[0]


class TestDryRunNoMutations:
    def test_dry_run_no_forge_calls(self, monkeypatch):
        """dry_run=True → no forge mutations but classifications populated."""
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
        assert len(result.classifications) == 1
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
        assert result.classifications == []
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
    def test_creates_ticket_when_no_existing_orphan_ticket(self, monkeypatch):
        """No prior orphan tickets → file a new tracking ticket."""
        s = _settings(orphaned_pr_dry_run=False)
        repo = _repo()
        svc = MagicMock()
        svc.get.return_value = None
        forge = _mock_forge(
            open_branches={"mill/20250101T000000Z-orphan-a1b2"},
        )
        _install_seams(monkeypatch, s, forge, svc)
        # recent_proposals_for already returns [] from _install_seams

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert svc.create.call_count == 1
        assert result.filed == 1
        assert result.skipped == 0
        title = svc.create.call_args.kwargs["title"]
        assert title == (
            "Track orphaned PR: test-owner/test-repo/mill/20250101T000000Z-orphan-a1b2"
        )

    def test_second_pass_is_noop_when_open_ticket_exists(self, monkeypatch):
        """When an open orphan ticket already exists → dedup skip, no create."""
        s = _settings(orphaned_pr_dry_run=False)
        repo = _repo()
        svc = MagicMock()
        svc.get.return_value = None
        forge = _mock_forge(
            open_branches={"mill/20250101T000000Z-orphan-a1b2"},
        )
        _install_seams(monkeypatch, s, forge, svc)

        existing = _ticket(
            ticket_id="20250101T000000Z-existing-a1b2",
            title=(
                "Track orphaned PR: test-owner/test-repo/"
                "mill/20250101T000000Z-orphan-a1b2"
            ),
            state=State.READY,
        )
        svc.recent_proposals_for.return_value = [existing]

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert svc.create.call_count == 0
        assert result.skipped >= 1
        assert result.filed == 0
        assert any("DEDUP_SKIP" in a for a in result.actions)

    def test_dedup_across_two_passes(self, monkeypatch):
        """First pass creates ticket; second pass dedup skips."""
        s = _settings(orphaned_pr_dry_run=False)
        repo = _repo()
        svc = MagicMock()
        svc.get.return_value = None
        forge = _mock_forge(
            open_branches={"mill/20250101T000000Z-orphan-a1b2"},
        )
        _install_seams(monkeypatch, s, forge, svc)

        existing = _ticket(
            ticket_id="20250101T000000Z-existing-a1b2",
            title=(
                "Track orphaned PR: test-owner/test-repo/"
                "mill/20250101T000000Z-orphan-a1b2"
            ),
            state=State.READY,
        )
        # First call: no existing tickets → file
        # Second call: existing ticket → dedup
        svc.recent_proposals_for.side_effect = [[], [existing]]

        # First pass
        result1 = run_orphaned_pr_check_pass(repo_config=repo)
        # Second pass
        result2 = run_orphaned_pr_check_pass(repo_config=repo)

        assert svc.create.call_count == 1
        assert result1.filed == 1
        assert result1.skipped == 0
        assert result2.filed == 0
        assert result2.skipped >= 1
        assert any("DEDUP_SKIP" in a for a in result2.actions)


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
        assert result.classifications == []
        forge.close_pr.assert_not_called()


class TestRepoConfigNone:
    def test_raises_on_none_repo_config(self):
        with pytest.raises(ValueError, match="requires a repo_config"):
            run_orphaned_pr_check_pass(repo_config=None)


# ---------------------------------------------------------------------------
# Author-guard tests
# ---------------------------------------------------------------------------


class TestHumanPrAuthorSkipped:
    def test_human_authored_mill_branch_skipped(self, monkeypatch):
        """PR with mill/ branch but non-bot author is skipped entirely."""
        s = _settings(orphaned_pr_dry_run=False)
        repo = _repo()
        svc = MagicMock()
        svc.get.return_value = None  # would normally file a ticket
        forge = _mock_forge(
            open_branches={"mill/20250101T000000Z-human-branch-a1b2"},
            bot_login="mill-bot",
        )
        # Override list_open_prs to return a human author on a mill-prefix branch
        forge.list_open_prs.return_value = [
            {
                "branch": "mill/20250101T000000Z-human-branch-a1b2",
                "author_login": "real-human",
            }
        ]
        forge.get_authenticated_user_login.return_value = "mill-bot"
        _install_seams(monkeypatch, s, forge, svc)

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert result.total_scanned == 1
        assert result.human_pr_skipped == 1
        assert result.closed == 0
        assert result.filed == 0
        forge.close_pr.assert_not_called()
        svc.create.assert_not_called()

    def test_bot_authored_mill_branch_processed(self, monkeypatch):
        """PR with mill/ branch and matching bot author is processed normally."""
        s = _settings(orphaned_pr_dry_run=False)
        repo = _repo()
        svc = MagicMock()
        svc.get.return_value = None  # orphan
        forge = _mock_forge(
            open_branches={"mill/20250101T000000Z-bot-branch-a1b2"},
            bot_login="mill-bot",
        )
        _install_seams(monkeypatch, s, forge, svc)

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert result.human_pr_skipped == 0
        # should have filed (non-empty diff from _mock_forge default)
        assert result.filed == 1

    def test_explicit_bot_logins_setting_used(self, monkeypatch):
        """orphaned_pr_bot_logins overrides auto-resolve; login in list → processed."""
        s = _settings(
            orphaned_pr_dry_run=False,
            orphaned_pr_bot_logins=["custom-bot"],
        )
        repo = _repo()
        svc = MagicMock()
        svc.get.return_value = None
        forge = _mock_forge(
            open_branches={"mill/20250101T000000Z-custom-bot-a1b2"},
            bot_login="custom-bot",
        )
        _install_seams(monkeypatch, s, forge, svc)

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert result.human_pr_skipped == 0
        # get_authenticated_user_login should NOT be called (explicit override)
        forge.get_authenticated_user_login.assert_not_called()
        assert result.filed == 1

    def test_explicit_bot_logins_setting_blocks_other_author(self, monkeypatch):
        """Author not in orphaned_pr_bot_logins → skipped even if prefix matches."""
        s = _settings(
            orphaned_pr_dry_run=False,
            orphaned_pr_bot_logins=["custom-bot"],
        )
        repo = _repo()
        svc = MagicMock()
        svc.get.return_value = None
        forge = _mock_forge(
            open_branches={"mill/20250101T000000Z-other-a1b2"},
        )
        forge.list_open_prs.return_value = [
            {
                "branch": "mill/20250101T000000Z-other-a1b2",
                "author_login": "someone-else",
            }
        ]
        _install_seams(monkeypatch, s, forge, svc)

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert result.human_pr_skipped == 1
        assert result.filed == 0


# ---------------------------------------------------------------------------
# Split action-cap tests
# ---------------------------------------------------------------------------


class TestSplitActionCap:
    def test_close_cap_does_not_block_file_actions(self, monkeypatch):
        """Close cap=1, file cap=5: once close cap hit, file actions still proceed."""
        s = _settings(
            orphaned_pr_dry_run=False,
            orphaned_pr_max_actions_per_pass=20,  # combined cap well above
            orphaned_pr_max_closes_per_pass=1,
            orphaned_pr_max_files_per_pass=5,
        )
        repo = _repo()
        svc = MagicMock()
        # 3 DONE tickets (→ close), 3 ERRORED tickets (→ file)
        tickets = {
            "mill/close-0001": _ticket("close-0001", state=State.DONE),
            "mill/close-0002": _ticket("close-0002", state=State.DONE),
            "mill/close-0003": _ticket("close-0003", state=State.DONE),
            "mill/file-0001": _ticket("file-0001", state=State.ERRORED),
            "mill/file-0002": _ticket("file-0002", state=State.ERRORED),
            "mill/file-0003": _ticket("file-0003", state=State.ERRORED),
        }

        def _get(ticket_id):
            branch = f"mill/{ticket_id}"
            return tickets.get(branch)

        svc.get.side_effect = _get
        forge = _mock_forge(open_branches=set(tickets))
        forge.list_open_prs.return_value = [
            {"branch": b, "author_login": "mill-bot"} for b in tickets
        ]
        forge.pr_files.return_value = [{"path": "x.py", "additions": 1, "deletions": 0}]
        _install_seams(monkeypatch, s, forge, svc)

        result = run_orphaned_pr_check_pass(repo_config=repo)

        # Only 1 close allowed, all 3 file-eligible ones proceed
        assert result.closed == 1
        assert result.filed == 3
        assert result.total_scanned == 6

    def test_file_cap_does_not_block_close_actions(self, monkeypatch):
        """File cap=1: file actions stop at 1 while closes continue."""
        s = _settings(
            orphaned_pr_dry_run=False,
            orphaned_pr_max_actions_per_pass=20,
            orphaned_pr_max_closes_per_pass=10,
            orphaned_pr_max_files_per_pass=1,
        )
        repo = _repo()
        svc = MagicMock()
        tickets = {
            "mill/file-0001": _ticket("file-0001", state=State.ERRORED),
            "mill/file-0002": _ticket("file-0002", state=State.ERRORED),
            "mill/close-0001": _ticket("close-0001", state=State.DONE),
            "mill/close-0002": _ticket("close-0002", state=State.DONE),
        }
        forge = _mock_forge(open_branches=set(tickets))
        forge.list_open_prs.return_value = [
            {"branch": b, "author_login": "mill-bot"} for b in tickets
        ]
        forge.pr_files.return_value = [{"path": "x.py", "additions": 1, "deletions": 0}]

        def _get(ticket_id):
            branch = f"mill/{ticket_id}"
            return tickets.get(branch)

        svc.get.side_effect = _get
        _install_seams(monkeypatch, s, forge, svc)

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert result.filed == 1
        assert result.closed == 2
        assert result.total_scanned == 4
