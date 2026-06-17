"""Unit tests for robotsix_mill.runners.credit_balance_runner."""

from unittest.mock import MagicMock, patch

from robotsix_mill.runners.credit_balance_runner import (
    check_credit_balance,
    run_credit_balance_check,
)


def _secrets_with(mgmt_key=None, api_key=None):
    secrets = MagicMock()
    secrets.openrouter_management_key = mgmt_key
    secrets.openrouter_api_key = api_key
    return secrets


def _fake_response(total_credits=100.0, total_usage=0.0):
    fake = MagicMock()
    fake.raise_for_status.return_value = None
    fake.json.return_value = {
        "data": {
            "total_credits": total_credits,
            "total_usage": total_usage,
        }
    }
    return fake


# ---------------------------------------------------------------------------
# check_credit_balance
# ---------------------------------------------------------------------------


class TestCheckCreditBalance:
    def test_no_key_configured_skips(self):
        with patch(
            "robotsix_mill.runners.credit_balance_runner.get_secrets",
            return_value=_secrets_with(),
        ):
            result = check_credit_balance()
            assert result.low is False
            assert result.error == "no OpenRouter key configured"

    def test_management_key_preferred(self):
        with (
            patch(
                "robotsix_mill.runners.credit_balance_runner.get_secrets",
                return_value=_secrets_with(mgmt_key="mgmt-key", api_key="api-key"),
            ),
            patch(
                "robotsix_mill.runners.credit_balance_runner.httpx.get",
                return_value=_fake_response(total_credits=100.0, total_usage=85.0),
            ),
        ):
            result = check_credit_balance()
            assert result.balance_usd == 15.0
            assert result.low is False
            assert result.error is None

    def test_api_key_fallback(self):
        with (
            patch(
                "robotsix_mill.runners.credit_balance_runner.get_secrets",
                return_value=_secrets_with(api_key="api-key"),
            ),
            patch(
                "robotsix_mill.runners.credit_balance_runner.httpx.get",
                return_value=_fake_response(total_credits=50.0, total_usage=47.0),
            ),
        ):
            result = check_credit_balance()
            assert result.balance_usd == 3.0
            assert result.low is True
            assert result.error is None

    def test_below_threshold_flags_low(self):
        with (
            patch(
                "robotsix_mill.runners.credit_balance_runner.get_secrets",
                return_value=_secrets_with(mgmt_key="k"),
            ),
            patch(
                "robotsix_mill.runners.credit_balance_runner.httpx.get",
                return_value=_fake_response(total_credits=50.0, total_usage=48.0),
            ),
        ):
            result = check_credit_balance()
            assert result.balance_usd == 2.0
            assert result.low is True

    def test_above_threshold_healthy(self):
        with (
            patch(
                "robotsix_mill.runners.credit_balance_runner.get_secrets",
                return_value=_secrets_with(mgmt_key="k"),
            ),
            patch(
                "robotsix_mill.runners.credit_balance_runner.httpx.get",
                return_value=_fake_response(total_credits=100.0, total_usage=20.0),
            ),
        ):
            result = check_credit_balance()
            assert result.balance_usd == 80.0
            assert result.low is False

    def test_api_error_returns_error_not_low(self):
        with (
            patch(
                "robotsix_mill.runners.credit_balance_runner.get_secrets",
                return_value=_secrets_with(mgmt_key="k"),
            ),
            patch(
                "robotsix_mill.runners.credit_balance_runner.httpx.get",
                side_effect=Exception("boom"),
            ),
        ):
            result = check_credit_balance()
            assert result.low is False
            assert "boom" in (result.error or "")

    def test_zero_balance_not_low_if_above_threshold(self):
        with (
            patch(
                "robotsix_mill.runners.credit_balance_runner.get_secrets",
                return_value=_secrets_with(mgmt_key="k"),
            ),
            patch(
                "robotsix_mill.runners.credit_balance_runner.httpx.get",
                return_value=_fake_response(total_credits=10.0, total_usage=10.0),
            ),
        ):
            result = check_credit_balance()
            assert result.balance_usd == 0.0
            assert result.low is True


# ---------------------------------------------------------------------------
# run_credit_balance_check
# ---------------------------------------------------------------------------


class TestRunCreditBalanceCheck:
    def test_no_key_skips_without_state_change(self):
        with patch(
            "robotsix_mill.runners.credit_balance_runner.get_secrets",
            return_value=_secrets_with(),
        ):
            from robotsix_mill.runtime.credit_status import clear_credit_status

            clear_credit_status()
            result = run_credit_balance_check()
            assert result.error == "no OpenRouter key configured"

    def test_healthy_balance_records_ok(self):
        with (
            patch(
                "robotsix_mill.runners.credit_balance_runner.get_secrets",
                return_value=_secrets_with(mgmt_key="k"),
            ),
            patch(
                "robotsix_mill.runners.credit_balance_runner.httpx.get",
                return_value=_fake_response(total_credits=100.0, total_usage=20.0),
            ),
        ):
            from robotsix_mill.runtime.credit_status import (
                clear_credit_status,
                get_credit_status,
            )

            clear_credit_status()
            result = run_credit_balance_check()
            assert result.low is False
            status = get_credit_status()
            assert status["low"] is False
            assert status["balance_usd"] == 80.0

    def test_low_balance_records_low(self):
        with (
            patch(
                "robotsix_mill.runners.credit_balance_runner.get_secrets",
                return_value=_secrets_with(mgmt_key="k"),
            ),
            patch(
                "robotsix_mill.runners.credit_balance_runner.httpx.get",
                return_value=_fake_response(total_credits=10.0, total_usage=9.0),
            ),
        ):
            from robotsix_mill.runtime.credit_status import get_credit_status

            result = run_credit_balance_check()
            assert result.low is True
            status = get_credit_status()
            assert status["low"] is True
            assert status["balance_usd"] == 1.0
