"""Unit tests for the scanner bulk auto-disposition policy.

Covers :mod:`robotsix_mill.stages.refine.scanner_disposition` (the pure
policy resolution) and the ``_scanner_auto_disposition`` /
``deterministic_triage_pre_checks`` wiring in
``robotsix_mill.stages.refine._triage`` — each disposition path
(auto-close-stale / auto-ready / hold-for-human) plus the dry-run gate.
No LLM/network collaborators are involved.
"""

from __future__ import annotations

import pytest

from robotsix_mill.config import RepoConfig, Settings
from robotsix_mill.core import db
from robotsix_mill.core.models import SourceKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.refine import _triage
from robotsix_mill.stages.refine.scanner_disposition import (
    DEFAULT_SCANNER_DISPOSITION,
    SCANNER_POLICY_SOURCES,
    ScannerDisposition,
    resolve_scanner_disposition,
)

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx_factory(tmp_path):
    counter = [0]

    def make(**env):
        db.reset_engine()
        s = Settings(data_dir=str(tmp_path / f"data{counter[0]}"), **env)
        db.init_db(s, board_id="test-board")
        svc = TicketService(s, board_id="test-board")
        counter[0] += 1
        return StageContext(
            settings=s,
            service=svc,
            repo_config=RepoConfig(
                repo_id="test-repo",
                board_id="test-board",
                langfuse_project_name="test",
                langfuse_public_key="pk-test",
                langfuse_secret_key="sk-test",
            ),
        )

    yield make
    db.reset_engine()


def _scanner_ticket(ctx, source, title="Finding", body="A scanner finding body."):
    return ctx.service.create(title, body, source=source)


# ===========================================================================
# resolve_scanner_disposition (pure policy)
# ===========================================================================


def test_resolve_returns_none_for_non_scanner_source():
    s = Settings(data_dir="/tmp/x")
    assert resolve_scanner_disposition(SourceKind.USER, s) is None
    assert resolve_scanner_disposition(SourceKind.CI, s) is None


def test_resolve_uses_builtin_default():
    s = Settings(data_dir="/tmp/x")
    assert (
        resolve_scanner_disposition(SourceKind.AUDIT, s)
        == ScannerDisposition.AUTO_READY
    )
    assert (
        resolve_scanner_disposition(SourceKind.MODULE_SIZE, s)
        == ScannerDisposition.AUTO_CLOSE_STALE
    )
    assert (
        resolve_scanner_disposition(SourceKind.HEALTH, s)
        == ScannerDisposition.HOLD_FOR_HUMAN
    )


def test_resolve_override_takes_precedence():
    s = Settings(
        data_dir="/tmp/x",
        scanner_auto_disposition_policy={SourceKind.AUDIT: "hold-for-human"},
    )
    assert (
        resolve_scanner_disposition(SourceKind.AUDIT, s)
        == ScannerDisposition.HOLD_FOR_HUMAN
    )


def test_resolve_unrecognised_override_falls_back_to_default():
    s = Settings(
        data_dir="/tmp/x",
        scanner_auto_disposition_policy={SourceKind.AUDIT: "bogus-value"},
    )
    assert (
        resolve_scanner_disposition(SourceKind.AUDIT, s)
        == ScannerDisposition.AUTO_READY
    )


def test_every_policy_source_has_a_default():
    for source in SCANNER_POLICY_SOURCES:
        assert source in DEFAULT_SCANNER_DISPOSITION


# ===========================================================================
# _scanner_auto_disposition (triage wiring)
# ===========================================================================


def test_dry_run_default_falls_through(ctx_factory):
    """Default dry-run mode logs only and returns None (no short-circuit)."""
    ctx = ctx_factory()  # scanner_auto_disposition_dry_run defaults to True
    assert ctx.settings.scanner_auto_disposition_dry_run is True
    t = _scanner_ticket(ctx, SourceKind.AUDIT)
    ws = ctx.service.workspace(t)
    assert (
        _triage._scanner_auto_disposition(ctx, t, "draft body", ws, ctx.settings)
        is None
    )


def test_non_scanner_source_falls_through(ctx_factory):
    ctx = ctx_factory(scanner_auto_disposition_dry_run=False)
    t = _scanner_ticket(ctx, SourceKind.USER)
    ws = ctx.service.workspace(t)
    assert (
        _triage._scanner_auto_disposition(ctx, t, "draft body", ws, ctx.settings)
        is None
    )


def test_auto_close_stale_closes(ctx_factory):
    ctx = ctx_factory(scanner_auto_disposition_dry_run=False)
    t = _scanner_ticket(ctx, SourceKind.MODULE_SIZE)
    ws = ctx.service.workspace(t)
    outcome = _triage._scanner_auto_disposition(ctx, t, "draft body", ws, ctx.settings)
    assert outcome is not None
    assert outcome.next_state == State.CLOSED
    assert "auto-close-stale" in (outcome.note or "")


def test_auto_ready_routes_to_implement(ctx_factory):
    ctx = ctx_factory(scanner_auto_disposition_dry_run=False)
    t = _scanner_ticket(ctx, SourceKind.AUDIT)
    ws = ctx.service.workspace(t)
    outcome = _triage._scanner_auto_disposition(
        ctx, t, "Edit `src/foo.py` for this.", ws, ctx.settings
    )
    assert outcome is not None
    assert outcome.next_state == State.READY
    assert "auto-ready" in (outcome.note or "")


def test_hold_for_human_routes_to_human_gate(ctx_factory):
    ctx = ctx_factory(scanner_auto_disposition_dry_run=False)
    t = _scanner_ticket(ctx, SourceKind.HEALTH)
    ws = ctx.service.workspace(t)
    outcome = _triage._scanner_auto_disposition(ctx, t, "draft body", ws, ctx.settings)
    assert outcome is not None
    assert outcome.next_state == State.HUMAN_ISSUE_APPROVAL
    assert "hold-for-human" in (outcome.note or "")


def test_override_changes_disposition_path(ctx_factory):
    """A per-source override flips an AUTO_READY source to auto-close."""
    ctx = ctx_factory(
        scanner_auto_disposition_dry_run=False,
        scanner_auto_disposition_policy={SourceKind.AUDIT: "auto-close-stale"},
    )
    t = _scanner_ticket(ctx, SourceKind.AUDIT)
    ws = ctx.service.workspace(t)
    outcome = _triage._scanner_auto_disposition(ctx, t, "draft body", ws, ctx.settings)
    assert outcome is not None
    assert outcome.next_state == State.CLOSED


# ===========================================================================
# deterministic_triage_pre_checks integration
# ===========================================================================


def test_pre_checks_short_circuits_on_enforced_policy(ctx_factory):
    ctx = ctx_factory(scanner_auto_disposition_dry_run=False)
    t = _scanner_ticket(ctx, SourceKind.MODULE_SIZE)
    ws = ctx.service.workspace(t)
    outcome = _triage.deterministic_triage_pre_checks(
        ctx, t, "A scanner finding body.", ws, ctx.settings
    )
    assert outcome is not None
    assert outcome.next_state == State.CLOSED


def test_pre_checks_falls_through_in_dry_run(ctx_factory):
    """Dry-run scanner ticket is not short-circuited by the policy (it may
    still match a later deterministic pre-check, but not the scanner one)."""
    ctx = ctx_factory()  # dry-run default True
    t = _scanner_ticket(ctx, SourceKind.MODULE_SIZE)
    ws = ctx.service.workspace(t)
    outcome = _triage.deterministic_triage_pre_checks(
        ctx,
        t,
        "A substantive scanner finding body with real content.",
        ws,
        ctx.settings,
    )
    # Not closed by the scanner policy; any non-None outcome here would be
    # from an unrelated pre-check — assert the scanner CLOSED path did not
    # fire.
    assert outcome is None or outcome.next_state != State.CLOSED
