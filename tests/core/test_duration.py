"""Tests for ``robotsix_mill.core.duration`` — human-readable interval
parsing/formatting."""

import pytest

try:
    from hypothesis import given, strategies as st

    _HYPOTHESIS_AVAILABLE = True
except ImportError:
    _HYPOTHESIS_AVAILABLE = False

from robotsix_mill.core.duration import format_duration, parse_duration


# ---------------------------------------------------------------------------
# parse_duration — valid inputs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("1w2d3h40m10s", 604800 + 172800 + 10800 + 2400 + 10),
        ("1w2d3h40m10s", 790810),
        ("1d", 86400),
        ("1w", 604800),
        ("12h", 43200),
        ("90m", 5400),
        ("2h30m10s", 9010),
        ("0s", 0),
        # int passthrough (seconds)
        (86400, 86400),
        (0, 0),
        # bare integer string treated as seconds
        ("3600", 3600),
        ("0", 0),
        # surrounding whitespace stripped
        ("  1d  ", 86400),
    ],
)
def test_parse_duration_valid(value, expected):
    assert parse_duration(value) == expected


# ---------------------------------------------------------------------------
# parse_duration — error cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "abc",
        "1x",
        "1d1d",  # duplicate unit
        "1.5h",  # non-numeric magnitude
        "1h2d",  # wrong (ascending) order
        -5,  # negative int
        True,  # bool rejected
    ],
)
def test_parse_duration_invalid(value):
    with pytest.raises(ValueError):
        parse_duration(value)


def test_parse_duration_error_message_names_value():
    with pytest.raises(ValueError, match="1x"):
        parse_duration("1x")


# ---------------------------------------------------------------------------
# format_duration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "seconds, expected",
    [
        (86400, "1d"),
        (604800, "1w"),
        (9010, "2h30m10s"),
        (0, "0s"),
        (3600, "1h"),
        (60, "1m"),
        (1, "1s"),
        (790810, "1w2d3h40m10s"),
    ],
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


def test_format_duration_negative_raises():
    with pytest.raises(ValueError):
        format_duration(-1)


# ---------------------------------------------------------------------------
# round-trip property
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "n", [0, 1, 60, 3600, 86400, 604800, 9010, 790810, 123456, 1000000]
)
def test_round_trip(n):
    assert parse_duration(format_duration(n)) == n


# ---------------------------------------------------------------------------
# property-based round-trip and format invariants
# ---------------------------------------------------------------------------


if _HYPOTHESIS_AVAILABLE:

    @given(st.integers(min_value=0, max_value=10**9))
    def test_duration_roundtrip_value(n):
        assert parse_duration(format_duration(n)) == n


    @given(st.integers(min_value=0, max_value=10**9))
    def test_duration_format_is_reparseable_string(n):
        s = format_duration(n)
        assert parse_duration(s) == n
