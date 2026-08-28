"""Tests for the operational-maintenance classifier."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from robotsix_mill.agents.ops_classify import OpsClassifyVerdict, run_ops_classify_agent


# ---------------------------------------------------------------------------
# OpsClassifyVerdict model
# ---------------------------------------------------------------------------
class TestOpsClassifyVerdict:
    def test_valid_operational(self) -> None:
        v = OpsClassifyVerdict(classification="OPERATIONAL", reason="token rotation")
        assert v.classification == "OPERATIONAL"
        assert v.reason == "token rotation"

    def test_valid_code(self) -> None:
        v = OpsClassifyVerdict(classification="CODE", reason="bug fix")
        assert v.classification == "CODE"
        assert v.reason == "bug fix"

    def test_invalid_classification(self) -> None:
        with pytest.raises(Exception):
            OpsClassifyVerdict(classification="UNKNOWN", reason="bad")


# ---------------------------------------------------------------------------
# run_ops_classify_agent
# ---------------------------------------------------------------------------
class TestRunOpsClassifyAgent:
    @patch("robotsix_mill.agents.yaml_loader.load_and_run_agent")
    def test_delegates_to_load_and_run(self, mock_load: MagicMock) -> None:
        verdict = OpsClassifyVerdict(
            classification="OPERATIONAL",
            reason="Manual redeploy.",
        )
        mock_load.return_value = MagicMock(output=verdict)

        settings = MagicMock()
        result = run_ops_classify_agent(
            settings=settings,
            title="Redeploy file-hub",
            body="Redeploy to pick up latest changes.",
        )

        assert result is verdict
        mock_load.assert_called_once()
        call_kwargs = mock_load.call_args[1]
        assert call_kwargs["definition_name"] == "ops_classify"
        assert call_kwargs["tools"] == []
        assert "Redeploy file-hub" in call_kwargs["prompt"]
