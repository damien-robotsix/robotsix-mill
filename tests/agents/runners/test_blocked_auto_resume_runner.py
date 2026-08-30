"""Tests for ``runners.blocked_auto_resume_runner``.

Real :class:`TicketService` on a ``tmp_path`` SQLite DB; no forge, no LLM.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import robotsix_mill.config as _cfg
from robotsix_mill.agents.runners import blocked_auto_resume_runner as bar
from robotsix_mill.config import (
    RepoConfig,
    ReposRegistry,
    Settings,
    _reset_repos_config,
)
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages.ci_fix_helpers import UPSTREAM_CI_BLOCK_MARKER

_BOARD = "test-board"


def _prepare(tmp_path, **env):
    db.reset_engine()
    settings = Settings(
        data_dir=str(tmp_path / "data"), require_approval="false", **env
    )
    db.init_db(settings, board_id=_BOARD)
    _reset_repos_config()
    rc = RepoConfig(
        repo_id="test-repo",
        board_id=_BOARD,
        langfuse_project_name="t",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
    )
    _cfg._repos_config = ReposRegistry(repos={rc.repo_id: rc})
    return settings, TicketService(settings, board_id=_BOARD)


def _blocked_ticket(service, note, title="t"):
    t = service.create(title=title, description="spec body long enough to count")
    service.transition(t.id, State.READY, note="approved")
    service.transition(t.id, State.BLOCKED, note=note)
    return service.get(t.id)


_LATER = datetime.now(UTC) + timedelta(hours=2)


def test_resumable_note_is_resumed_after_cooldown(tmp_path):
    settings, service = _prepare(tmp_path)
    t = _blocked_ticket(
        service, "agent error — resumable: You've hit your session limit"
    )

    result = bar.run_blocked_auto_resume(settings, now=_LATER)

    assert result["resumed"] == 1
    fresh = service.get(t.id)
    assert fresh.state is State.READY
    assert fresh.blocked_from is None
    comments = service.list_comments(t.id)
    assert any((c.body or "").startswith("[auto-resume 1/1]") for c in comments)


def test_fresh_block_is_left_cooling(tmp_path):
    settings, service = _prepare(tmp_path)
    t = _blocked_ticket(service, "stage timed out after 2400s — resumable")

    result = bar.run_blocked_auto_resume(settings)  # now ≈ block time

    assert result == {
        "resumed": 0,
        "cooling": 1,
        "budget_exhausted": 0,
        "not_matched": 0,
    }
    assert service.get(t.id).state is State.BLOCKED


def test_budget_is_one_resume_per_ticket_by_default(tmp_path):
    settings, service = _prepare(tmp_path)
    t = _blocked_ticket(
        service, "ci fix agent could not turn CI green within its iteration budget"
    )

    assert bar.run_blocked_auto_resume(settings, now=_LATER)["resumed"] == 1
    # …it blocks again for the same reason
    service.transition(
        t.id,
        State.BLOCKED,
        note="ci fix agent could not turn CI green within its iteration budget",
    )

    result = bar.run_blocked_auto_resume(settings, now=_LATER + timedelta(hours=2))

    assert result["resumed"] == 0
    assert result["budget_exhausted"] == 1
    assert service.get(t.id).state is State.BLOCKED


def test_fingerprint_and_upstream_parks_are_never_touched(tmp_path):
    settings, service = _prepare(tmp_path)
    a = _blocked_ticket(
        service,
        "implement — resumable: spec unchanged since last spec-determined implement attempt",
        "a",
    )
    b = _blocked_ticket(
        service, f"{UPSTREAM_CI_BLOCK_MARKER}: main is red — resumable", "b"
    )
    c = _blocked_ticket(service, "scope triage REJECT — operator decision needed", "c")

    result = bar.run_blocked_auto_resume(settings, now=_LATER)

    assert result["resumed"] == 0
    assert result["not_matched"] == 3
    for t in (a, b, c):
        assert service.get(t.id).state is State.BLOCKED


def test_disabled_when_no_patterns_or_zero_budget(tmp_path):
    settings, service = _prepare(tmp_path, blocked_auto_resume_max_per_ticket=0)
    _blocked_ticket(service, "agent error — resumable: boom")
    assert bar.run_blocked_auto_resume(settings, now=_LATER)["resumed"] == 0


def test_matches_helper_is_case_insensitive_and_ignores_bad_patterns():
    assert bar._matches("Agent Error — Resumable: x", ["agent error — resumable"])
    assert not bar._matches("agent error — resumable: spec unchanged", ["— resumable"])
    assert bar._matches("stage timed out", ["([unclosed", "timed out"])


def test_default_patterns_cover_scope_triage_agent_error():
    """Tickets already BLOCKED by the implement stage's scope-triage error
    fall-through (infra errors swallowed before the fix) get the one
    automatic retry from the default pattern list."""
    from robotsix_mill.config._settings_periodic import _PeriodicSettings

    patterns = _PeriodicSettings.model_fields["blocked_auto_resume_patterns"].default
    note = (
        "scope-triage agent error (ClaudeSDKUsageExhaustedError: You've hit "
        "your session limit · resets 12pm (UTC)) — escalated for human review; "
        "resume-blocked re-runs the triage — out-of-scope: `a.py`"
    )
    assert bar._matches(note, patterns)
