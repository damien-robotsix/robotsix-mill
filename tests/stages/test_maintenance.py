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
    resolves. The real module is created in a follow-up ticket."""
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
            mock_run.return_value = mock_result

            outcome = stage.run(ticket, ctx)

            assert outcome.next_state == State.BLOCKED
            assert "rate limited" in outcome.note
        finally:
            _remove_mock_maintenance_agent()
