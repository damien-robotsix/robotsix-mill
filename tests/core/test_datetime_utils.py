"""Tests for ``robotsix_mill.core.datetime_utils`` — TZDateTime type
decorator and ``_as_utc`` helper."""

from datetime import datetime, timedelta, timezone

import pytest

from robotsix_mill.core.datetime_utils import TZDateTime, _as_utc


# ---------------------------------------------------------------------------
# TZDateTime.process_bind_param
# ---------------------------------------------------------------------------

def test_bind_param_raises_on_naive():
    """Naive datetime → TypeError with "timezone-aware" message."""
    tz_dt = TZDateTime()
    naive = datetime(2025, 1, 1)
    with pytest.raises(TypeError, match="timezone-aware"):
        tz_dt.process_bind_param(naive, dialect=None)


def test_bind_param_converts_aware_to_naive_utc():
    """Aware UTC input → naive UTC output (tzinfo stripped)."""
    tz_dt = TZDateTime()
    aware_utc = datetime(2025, 1, 1, tzinfo=timezone.utc)
    result = tz_dt.process_bind_param(aware_utc, dialect=None)
    assert result.tzinfo is None
    assert result == datetime(2025, 1, 1)  # naive, same wall time


def test_bind_param_converts_non_utc_to_naive_utc():
    """Aware non-UTC (EST, UTC-5) → correctly shifted naive UTC."""
    tz_dt = TZDateTime()
    est = timezone(timedelta(hours=-5))
    aware_est = datetime(2025, 1, 1, 12, 0, 0, tzinfo=est)  # noon EST
    result = tz_dt.process_bind_param(aware_est, dialect=None)
    assert result.tzinfo is None
    # noon EST = 5 PM UTC
    assert result.hour == 17
    assert result.day == 1
    assert result == datetime(2025, 1, 1, 17, 0, 0)


# ---------------------------------------------------------------------------
# TZDateTime.process_result_value
# ---------------------------------------------------------------------------

def test_result_value_reattaches_utc():
    """Naive DB value → aware UTC."""
    tz_dt = TZDateTime()
    naive = datetime(2025, 1, 1)
    result = tz_dt.process_result_value(naive, dialect=None)
    assert result.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# None passthrough (both methods)
# ---------------------------------------------------------------------------

def test_none_passthrough():
    """None passes through both process_bind_param and process_result_value."""
    tz_dt = TZDateTime()
    assert tz_dt.process_bind_param(None, dialect=None) is None
    assert tz_dt.process_result_value(None, dialect=None) is None


# ---------------------------------------------------------------------------
# _as_utc helper
# ---------------------------------------------------------------------------

def test_as_utc_identity_and_coercion():
    """_as_utc: aware passes through unchanged (identity); naive gets UTC."""
    aware = datetime(2025, 1, 1, tzinfo=timezone.utc)
    naive = datetime(2025, 1, 1)

    # Aware: identity (same object)
    assert _as_utc(aware) is aware

    # Naive: coerced to UTC
    coerced = _as_utc(naive)
    assert coerced.tzinfo == timezone.utc
    assert coerced == datetime(2025, 1, 1, tzinfo=timezone.utc)
