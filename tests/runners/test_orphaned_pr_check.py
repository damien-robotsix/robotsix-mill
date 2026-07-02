"""Tests for the orphaned-PR check runner."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from robotsix_mill.config import RepoConfig, Settings
from robotsix_mill.core.models import SourceKind, Ticket
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
    open_prs=None,
    bot_login="mill-bot[bot]",
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
    forge.list_open_prs.return_value = open_prs if open_prs is not None else []
    forge.get_authenticated_user_login.return_value = bot_login
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


# ---------------------------------------------------------------------------
# Integration tests — human author guard
# ---------------------------------------------------------------------------

HUMAN_BRANCH = "mill/20250101T000000Z-orphan-a1b2"
BOT_BRANCH = "mill/20250101T000000Z-bot-c3d4"
BOT_LOGIN = "mill-bot[bot]"
HUMAN_LOGIN = "alice"


class TestHumanAuthorGuard:
    """Author-guard tests: human-authored mill/-prefixed PRs must be skipped.

    Each test wires seams via ``_install_seams``.  The branch name used
    in every scenario resolves to ``None`` from ``svc.get`` so the PR
    would be classified and acted on if the author guard were absent.
    """

    def test_human_authored_mill_branch_skipped_by_default(self, monkeypatch):
        """Human author on a mill/ branch → skipped when bot-login resolved from forge."""
        s = _settings(orphaned_pr_bot_logins=[])
        repo = _repo()
        svc = MagicMock()
        svc.get.return_value = None
        forge = _mock_forge(
            open_prs=[{"branch": HUMAN_BRANCH, "author_login": HUMAN_LOGIN}],
            bot_login=BOT_LOGIN,
        )
        _install_seams(monkeypatch, s, forge, svc)

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert result.closed == 0
        assert result.filed == 0
        forge.close_pr.assert_not_called()
        svc.create.assert_not_called()
        assert forge.get_authenticated_user_login.call_count >= 1

    def test_bot_authored_mill_branch_is_processed(self, monkeypatch):
        """Bot-authored mill/ branch → processed normally."""
        s = _settings(orphaned_pr_bot_logins=[])
        repo = _repo()
        svc = MagicMock()
        svc.get.return_value = None
        forge = _mock_forge(
            open_prs=[{"branch": BOT_BRANCH, "author_login": BOT_LOGIN}],
            bot_login=BOT_LOGIN,
        )
        _install_seams(monkeypatch, s, forge, svc)

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert result.filed + result.closed >= 1
        assert svc.create.call_count + forge.close_pr.call_count >= 1

    def test_explicit_bot_logins_config_excludes_human(self, monkeypatch):
        """Explicit bot-login list → human excluded; forge login resolution skipped."""
        s = _settings(
            orphaned_pr_bot_logins=[BOT_LOGIN, "org-automation[bot]"],
        )
        repo = _repo()
        svc = MagicMock()
        svc.get.return_value = None
        forge = _mock_forge(
            open_prs=[{"branch": HUMAN_BRANCH, "author_login": HUMAN_LOGIN}],
        )
        _install_seams(monkeypatch, s, forge, svc)

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert result.closed == 0
        assert result.filed == 0
        forge.get_authenticated_user_login.assert_not_called()

    def test_fail_open_when_login_resolution_fails(self, monkeypatch):
        """Empty bot-login list + forge returns '' → fail-open: branch IS processed."""
        s = _settings(orphaned_pr_bot_logins=[])
        repo = _repo()
        svc = MagicMock()
        svc.get.return_value = None
        forge = _mock_forge(
            open_prs=[{"branch": HUMAN_BRANCH, "author_login": HUMAN_LOGIN}],
            bot_login="",
        )
        _install_seams(monkeypatch, s, forge, svc)

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert result.filed + result.closed >= 1
        assert svc.create.call_count + forge.close_pr.call_count >= 1

    def test_sentinel_human_pr_not_acted_on_bot_pr_is(self, monkeypatch):
        """Guard-removal sentinel: removing author check causes human PR to be
        acted on, making filed+closed == 2 instead of 1."""
        s = _settings(
            orphaned_pr_bot_logins=[],
            orphaned_pr_max_actions_per_pass=5,
        )
        repo = _repo()
        svc = MagicMock()
        svc.get.return_value = None
        forge = _mock_forge(
            open_prs=[
                {"branch": HUMAN_BRANCH, "author_login": HUMAN_LOGIN},
                {"branch": BOT_BRANCH, "author_login": BOT_LOGIN},
            ],
            bot_login=BOT_LOGIN,
        )
        _install_seams(monkeypatch, s, forge, svc)

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert result.filed + result.closed == 1
        assert svc.create.call_count + forge.close_pr.call_count == 1


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
        svc.recent_proposals_for.side_effect = [[], [], [existing], [existing]]

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
            {"branch": b, "author_login": "mill-bot[bot]"} for b in tickets
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
            {"branch": b, "author_login": "mill-bot[bot]"} for b in tickets
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


# ---------------------------------------------------------------------------
# Foreign (non-board) PR tracking
# ---------------------------------------------------------------------------


def _foreign_pr(
    number: int,
    branch: str = "dependabot/pip/requests-2.32.0",
    author: str = "dependabot[bot]",
):
    return {
        "branch": branch,
        "author_login": author,
        "number": number,
        "url": f"https://github.com/test-owner/test-repo/pull/{number}",
        "title": f"Bump requests from 2.31.0 to 2.32.0 (#{number})",
    }


class TestForeignPrTracking:
    def test_flag_off_foreign_pr_untouched(self, monkeypatch):
        """Default (flag off): a foreign PR is never scanned, filed, or closed."""
        s = _settings(orphaned_pr_dry_run=False)  # flag defaults False
        repo = _repo()
        svc = MagicMock()
        svc.get.return_value = None
        forge = _mock_forge(open_prs=[_foreign_pr(101)])
        _install_seams(monkeypatch, s, forge, svc)

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert result.total_scanned == 0  # no mill/ branches
        assert result.foreign_filed == 0
        assert result.foreign_skipped == 0
        svc.create.assert_not_called()
        forge.close_pr.assert_not_called()

    def test_flag_on_files_one_ticket_for_foreign_pr(self, monkeypatch):
        """Flag on: foreign PR with no ticket → exactly one tracking ticket."""
        s = _settings(orphaned_pr_dry_run=False, orphaned_pr_track_foreign_prs=True)
        repo = _repo()
        svc = MagicMock()
        svc.get.return_value = None
        forge = _mock_forge(open_prs=[_foreign_pr(101)])
        _install_seams(monkeypatch, s, forge, svc)

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert result.foreign_filed == 1
        assert svc.create.call_count == 1
        title = svc.create.call_args.kwargs["title"]
        assert title == "Track external PR: test-owner/test-repo#101"
        body = svc.create.call_args.kwargs["description"]
        assert "dependabot[bot]" in body
        assert "dependabot/pip/requests-2.32.0" in body
        assert "pull/101" in body
        assert "Bump requests" in body
        # never closes a foreign PR
        forge.close_pr.assert_not_called()
        forge.post_pr_comment.assert_not_called()

    def test_foreign_pr_never_closed(self, monkeypatch):
        """Even an empty-diff / conflicting foreign PR is only filed, never closed."""
        s = _settings(orphaned_pr_dry_run=False, orphaned_pr_track_foreign_prs=True)
        repo = _repo()
        svc = MagicMock()
        svc.get.return_value = None
        forge = _mock_forge(
            open_prs=[_foreign_pr(202)],
            pr_files=[],  # empty diff would close a mill PR
            pr_status={"state": "open", "mergeable": False},  # conflicting
        )
        _install_seams(monkeypatch, s, forge, svc)

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert result.foreign_filed == 1
        assert result.closed == 0
        forge.close_pr.assert_not_called()

    def test_second_pass_is_noop_dedup(self, monkeypatch):
        """A foreign tracking ticket already open → dedup skip, no create."""
        s = _settings(orphaned_pr_dry_run=False, orphaned_pr_track_foreign_prs=True)
        repo = _repo()
        svc = MagicMock()
        svc.get.return_value = None
        forge = _mock_forge(open_prs=[_foreign_pr(303)])
        _install_seams(monkeypatch, s, forge, svc)

        existing = _ticket(
            ticket_id="20250101T000000Z-existing-a1b2",
            title="Track external PR: test-owner/test-repo#303",
            state=State.READY,
        )
        svc.recent_proposals_for.return_value = [existing]

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert svc.create.call_count == 0
        assert result.foreign_filed == 0
        assert result.foreign_skipped >= 1
        assert any("DEDUP_SKIP" in a and "foreign_pr" in a for a in result.actions)

    def test_idempotent_across_two_passes(self, monkeypatch):
        """First pass files; second pass (ticket now open) dedup skips."""
        s = _settings(orphaned_pr_dry_run=False, orphaned_pr_track_foreign_prs=True)
        repo = _repo()
        svc = MagicMock()
        svc.get.return_value = None
        forge = _mock_forge(open_prs=[_foreign_pr(404)])
        _install_seams(monkeypatch, s, forge, svc)

        existing = _ticket(
            ticket_id="20250101T000000Z-existing-a1b2",
            title="Track external PR: test-owner/test-repo#404",
            state=State.READY,
        )
        svc.recent_proposals_for.side_effect = [
            [],
            [],
            [],
            [existing],
            [existing],
            [existing],
        ]

        result1 = run_orphaned_pr_check_pass(repo_config=repo)
        result2 = run_orphaned_pr_check_pass(repo_config=repo)

        assert svc.create.call_count == 1
        assert result1.foreign_filed == 1
        assert result2.foreign_filed == 0
        assert result2.foreign_skipped >= 1

    def test_dedup_includes_terminal_states(self, monkeypatch):
        """A DONE tracking ticket also dedup-suppresses a foreign PR re-file."""
        s = _settings(orphaned_pr_dry_run=False, orphaned_pr_track_foreign_prs=True)
        repo = _repo()
        svc = MagicMock()
        svc.get.return_value = None
        forge = _mock_forge(open_prs=[_foreign_pr(505)])
        _install_seams(monkeypatch, s, forge, svc)

        done_ticket = _ticket(
            ticket_id="20250101T000000Z-done-tracker-a1b2",
            title="Track external PR: test-owner/test-repo#505",
            state=State.DONE,
            source=SourceKind.ORPHANED_PR_CHECK,
        )
        svc.recent_proposals_for.return_value = [done_ticket]

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert svc.create.call_count == 0
        assert result.foreign_filed == 0
        assert result.foreign_skipped >= 1
        assert any("DEDUP_SKIP" in a and "foreign_pr" in a for a in result.actions)

    def test_dedup_includes_closed_state(self, monkeypatch):
        """A CLOSED tracking ticket also dedup-suppresses a foreign PR re-file."""
        s = _settings(orphaned_pr_dry_run=False, orphaned_pr_track_foreign_prs=True)
        repo = _repo()
        svc = MagicMock()
        svc.get.return_value = None
        forge = _mock_forge(open_prs=[_foreign_pr(606)])
        _install_seams(monkeypatch, s, forge, svc)

        closed_ticket = _ticket(
            ticket_id="20250101T000000Z-closed-tracker-a1b2",
            title="Track external PR: test-owner/test-repo#606",
            state=State.CLOSED,
            source=SourceKind.ORPHANED_PR_CHECK,
        )
        svc.recent_proposals_for.return_value = [closed_ticket]

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert svc.create.call_count == 0
        assert result.foreign_filed == 0
        assert result.foreign_skipped >= 1
        assert any("DEDUP_SKIP" in a and "foreign_pr" in a for a in result.actions)

    def test_file_cap_limits_foreign_tickets(self, monkeypatch):
        """Many foreign PRs, max_files_per_pass=2 → only 2 filed."""
        s = _settings(
            orphaned_pr_dry_run=False,
            orphaned_pr_track_foreign_prs=True,
            orphaned_pr_max_actions_per_pass=20,
            orphaned_pr_max_files_per_pass=2,
        )
        repo = _repo()
        svc = MagicMock()
        svc.get.return_value = None
        open_prs = [
            _foreign_pr(500 + i, branch=f"dependabot/pip/lib-{i}") for i in range(6)
        ]
        forge = _mock_forge(open_prs=open_prs)
        _install_seams(monkeypatch, s, forge, svc)

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert result.foreign_filed == 2
        assert svc.create.call_count == 2
        assert any("foreign action cap reached" in a for a in result.actions)

    def test_combined_cap_shared_with_mill_actions(self, monkeypatch):
        """Mill file actions consume the combined cap before foreign PRs run."""
        s = _settings(
            orphaned_pr_dry_run=False,
            orphaned_pr_track_foreign_prs=True,
            orphaned_pr_max_actions_per_pass=1,
            orphaned_pr_max_files_per_pass=5,
        )
        repo = _repo()
        svc = MagicMock()
        svc.get.return_value = None  # mill orphan → files a ticket
        forge = _mock_forge(
            open_prs=[
                {
                    "branch": "mill/20250101T000000Z-orphan-a1b2",
                    "author_login": "mill-bot[bot]",
                },
                _foreign_pr(600),
            ],
            bot_login="mill-bot[bot]",
        )
        _install_seams(monkeypatch, s, forge, svc)

        result = run_orphaned_pr_check_pass(repo_config=repo)

        # combined cap of 1 consumed by the mill file → foreign PR deferred
        assert result.filed == 1
        assert result.foreign_filed == 0
        assert any("foreign action cap reached" in a for a in result.actions)

    def test_dry_run_no_mutations(self, monkeypatch):
        """Dry-run: foreign intent logged, no ticket created."""
        s = _settings(orphaned_pr_dry_run=True, orphaned_pr_track_foreign_prs=True)
        repo = _repo()
        svc = MagicMock()
        svc.get.return_value = None
        forge = _mock_forge(open_prs=[_foreign_pr(700)])
        _install_seams(monkeypatch, s, forge, svc)

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert result.foreign_filed == 0
        assert result.foreign_skipped == 1
        svc.create.assert_not_called()
        forge.close_pr.assert_not_called()
        assert any("FILE_TICKET" in a and "foreign_pr" in a for a in result.actions)

    def test_fallback_title_when_number_missing(self, monkeypatch):
        """Foreign PR dict without a number → branch-based title."""
        s = _settings(orphaned_pr_dry_run=False, orphaned_pr_track_foreign_prs=True)
        repo = _repo()
        svc = MagicMock()
        svc.get.return_value = None
        forge = _mock_forge(
            open_prs=[
                {
                    "branch": "feature/no-number",
                    "author_login": "alice",
                    "number": None,
                    "url": "",
                    "title": "",
                }
            ]
        )
        _install_seams(monkeypatch, s, forge, svc)

        result = run_orphaned_pr_check_pass(repo_config=repo)

        assert result.foreign_filed == 1
        title = svc.create.call_args.kwargs["title"]
        assert title == "Track external PR: test-owner/test-repo/feature/no-number"


# ---------------------------------------------------------------------------
# Reconcile closed-tracker-PR tests
# ---------------------------------------------------------------------------


class TestReconcileClosedTrackers:
    def test_closes_stale_tracker_by_pr_number(self, monkeypatch):
        """Tracker ticket whose PR number is absent from open_prs → close_tracker."""
        s = _settings(orphaned_pr_dry_run=False)
        repo = _repo()
        svc = MagicMock()
        svc.get.return_value = None

        tracker = _ticket(
            ticket_id="20250101T000000Z-tracker-a1b2",
            title="Track external PR: test-owner/test-repo#42",
            state=State.READY,
            source=SourceKind.ORPHANED_PR_CHECK,
        )
        # open_prs does NOT include PR #42
        forge = _mock_forge(
            open_prs=[
                {
                    "branch": "mill/some-other-pr",
                    "author_login": "mill-bot[bot]",
                    "number": 99,
                }
            ],
            bot_login="mill-bot[bot]",
        )
        _install_seams(monkeypatch, s, forge, svc)
        svc.recent_proposals_for.return_value = [tracker]

        result = run_orphaned_pr_check_pass(repo_config=repo)

        svc.close_tracker.assert_called_once_with(
            tracker.id,
            note="Tracked PR is no longer open — auto-closed by reconcile pass",
        )
        assert any("CLOSE_TRACKER" in a for a in result.actions)

    def test_empty_open_prs_skips_reconcile(self, monkeypatch):
        """When open_prs is empty, _reconcile_closed_tracker_prs is not called
        and no tracker tickets are closed."""
        s = _settings(orphaned_pr_dry_run=False)
        repo = _repo()
        svc = MagicMock()
        svc.get.return_value = None

        tracker = _ticket(
            ticket_id="20250101T000000Z-tracker-a1b2",
            title="Track external PR: test-owner/test-repo#42",
            state=State.READY,
            source=SourceKind.ORPHANED_PR_CHECK,
        )
        forge = _mock_forge(open_prs=[])
        _install_seams(monkeypatch, s, forge, svc)
        svc.recent_proposals_for.return_value = [tracker]

        run_orphaned_pr_check_pass(repo_config=repo)

        svc.close_tracker.assert_not_called()

    def test_cross_repo_title_not_closed(self, monkeypatch):
        """A tracker ticket whose title references a different repo_id
        is not matched and not closed."""
        s = _settings(orphaned_pr_dry_run=False)
        repo = _repo()
        svc = MagicMock()
        svc.get.return_value = None

        # Title references "other-owner/other-repo", not "test-owner/test-repo"
        other_tracker = _ticket(
            ticket_id="20250101T000000Z-other-a1b2",
            title="Track external PR: other-owner/other-repo#42",
            state=State.READY,
            source=SourceKind.ORPHANED_PR_CHECK,
        )
        forge = _mock_forge(
            open_prs=[
                {
                    "branch": "mill/some-pr",
                    "author_login": "mill-bot[bot]",
                    "number": 99,
                }
            ],
            bot_login="mill-bot[bot]",
        )
        _install_seams(monkeypatch, s, forge, svc)
        svc.recent_proposals_for.return_value = [other_tracker]

        run_orphaned_pr_check_pass(repo_config=repo)

        svc.close_tracker.assert_not_called()
