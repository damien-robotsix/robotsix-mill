"""Tests for MaintenanceStage."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock

from robotsix_mill.core.states import State
from robotsix_mill.stages.maintenance import MaintenanceStage


def _inject_mock_maintenance_agent():
    """Inject a mock ``robotsix_mill.agents.maintenance`` module into
    ``sys.modules`` so the lazy import inside ``MaintenanceStage.run()``
    resolves. Mock the real module so unit tests don't require an LLM."""
    mock_agent_mod = ModuleType("robotsix_mill.agents.maintenance")
    mock_agent_mod.run_maintenance_agent = MagicMock()
    sys.modules["robotsix_mill.agents.maintenance"] = mock_agent_mod
    return mock_agent_mod.run_maintenance_agent


def _remove_mock_maintenance_agent():
    """Remove the injected mock module so it doesn't leak into other tests."""
    sys.modules.pop("robotsix_mill.agents.maintenance", None)


class TestMaintenanceStage:
    def test_name_and_input_state(self):
        stage = MaintenanceStage()
        assert stage.name == "maintenance"
        assert stage.input_state == State.MAINTENANCE
        assert stage.traced is True

    def test_run_success_returns_done(self):
        """When the maintenance agent reports success, the stage
        returns DONE."""
        stage = MaintenanceStage()
        ticket = MagicMock()
        ctx = MagicMock()

        mock_run = _inject_mock_maintenance_agent()
        try:
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.note = "repo created"
            mock_result.redirect_to = None
            mock_run.return_value = mock_result

            outcome = stage.run(ticket, ctx)

            assert outcome.next_state == State.DONE
            assert outcome.note == "repo created"
            mock_run.assert_called_once_with(ticket, ctx)
        finally:
            _remove_mock_maintenance_agent()

    def test_run_failure_returns_blocked(self):
        """When the maintenance agent reports failure, the stage
        escalates to BLOCKED."""
        stage = MaintenanceStage()
        ticket = MagicMock()
        ctx = MagicMock()

        mock_run = _inject_mock_maintenance_agent()
        try:
            mock_result = MagicMock()
            mock_result.success = False
            mock_result.note = "fork failed: rate limited"
            mock_result.redirect_to = None
            mock_run.return_value = mock_result

            outcome = stage.run(ticket, ctx)

            assert outcome.next_state == State.BLOCKED
            assert "rate limited" in outcome.note
        finally:
            _remove_mock_maintenance_agent()

    def test_run_redirect_to_ready(self):
        """When the agent sets redirect_to=READY, the stage returns
        Outcome(State.READY) regardless of success."""
        stage = MaintenanceStage()
        ticket = MagicMock()
        ctx = MagicMock()

        mock_run = _inject_mock_maintenance_agent()
        try:
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.note = "Needs code fix in repo X"
            mock_result.redirect_to = State.READY
            mock_run.return_value = mock_result

            outcome = stage.run(ticket, ctx)

            assert outcome.next_state == State.READY
            assert outcome.note == "Needs code fix in repo X"
        finally:
            _remove_mock_maintenance_agent()

    def test_run_redirect_overrides_failure(self):
        """Even when success=False, redirect_to takes precedence and
        returns the redirect target."""
        stage = MaintenanceStage()
        ticket = MagicMock()
        ctx = MagicMock()

        mock_run = _inject_mock_maintenance_agent()
        try:
            mock_result = MagicMock()
            mock_result.success = False
            mock_result.note = "Investigation: not operational, needs code"
            mock_result.redirect_to = State.READY
            mock_run.return_value = mock_result

            outcome = stage.run(ticket, ctx)

            assert outcome.next_state == State.READY
            assert outcome.note == "Investigation: not operational, needs code"
        finally:
            _remove_mock_maintenance_agent()

    def test_run_redirect_to_draft(self):
        """redirect_to=DRAFT is also supported."""
        stage = MaintenanceStage()
        ticket = MagicMock()
        ctx = MagicMock()

        mock_run = _inject_mock_maintenance_agent()
        try:
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.note = "Needs re-drafting"
            mock_result.redirect_to = State.DRAFT
            mock_run.return_value = mock_result

            outcome = stage.run(ticket, ctx)

            assert outcome.next_state == State.DRAFT
            assert outcome.note == "Needs re-drafting"
        finally:
            _remove_mock_maintenance_agent()

    def test_run_no_redirect_still_returns_done_on_success(self):
        """When redirect_to is None (not set), existing behavior is
        unchanged: success → DONE."""
        stage = MaintenanceStage()
        ticket = MagicMock()
        ctx = MagicMock()

        mock_run = _inject_mock_maintenance_agent()
        try:
            mock_result = MagicMock()
            mock_result.success = True
            mock_result.note = "repo created"
            mock_result.redirect_to = None
            mock_run.return_value = mock_result

            outcome = stage.run(ticket, ctx)

            assert outcome.next_state == State.DONE
            assert outcome.note == "repo created"
        finally:
            _remove_mock_maintenance_agent()
