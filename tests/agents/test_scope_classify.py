"""Tests for the scope-breadth (task-vs-epic) classifier."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from robotsix_mill.agents.scope_classify import ScopeVerdict, run_scope_classify_agent


# ---------------------------------------------------------------------------
# ScopeVerdict model
# ---------------------------------------------------------------------------
class TestScopeVerdict:
    def test_valid_task(self) -> None:
        v = ScopeVerdict(classification="TASK", confidence=0.1, reason="one fix")
        assert v.classification == "TASK"
        assert v.confidence == 0.1
        assert v.reason == "one fix"

    def test_valid_epic(self) -> None:
        v = ScopeVerdict(classification="EPIC", confidence=0.9, reason="many parts")
        assert v.classification == "EPIC"
        assert v.confidence == 0.9

    def test_invalid_classification(self) -> None:
        with pytest.raises(Exception):
            ScopeVerdict(classification="UNKNOWN", confidence=0.5, reason="bad")

    def test_confidence_out_of_range(self) -> None:
        with pytest.raises(Exception):
            ScopeVerdict(classification="EPIC", confidence=1.5, reason="bad")


# ---------------------------------------------------------------------------
# run_scope_classify_agent
# ---------------------------------------------------------------------------
class TestRunScopeClassifyAgent:
    @patch("robotsix_mill.agents.yaml_loader.load_and_run_agent")
    def test_delegates_to_load_and_run(self, mock_load: MagicMock) -> None:
        verdict = ScopeVerdict(
            classification="EPIC",
            confidence=0.85,
            reason="Bundles three independent subsystems.",
        )
        mock_load.return_value = MagicMock(output=verdict)

        settings = MagicMock()
        result = run_scope_classify_agent(
            settings=settings,
            title="Build the whole notifications subsystem",
            body="Add email, SMS, and webhook channels plus a preferences UI.",
        )

        assert result is verdict
        mock_load.assert_called_once()
        call_kwargs = mock_load.call_args[1]
        assert call_kwargs["definition_name"] == "scope_classify"
        assert call_kwargs["tools"] == []
        assert "notifications subsystem" in call_kwargs["prompt"]
