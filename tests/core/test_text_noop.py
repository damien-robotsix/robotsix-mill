"""Shared no-op detector used by retrospect + report_issue."""

import pytest

from robotsix_mill.core.text_noop import NOOP_MARKERS, is_noop_report


@pytest.mark.parametrize("marker", NOOP_MARKERS)
def test_each_marker_detected(marker):
    # Embed the marker in a realistic title (not the bare marker).
    assert is_noop_report(f"Retrospect: {marker} for this ticket")


@pytest.mark.parametrize("title", ["", "   ", None])
def test_empty_is_noop(title):
    assert is_noop_report(title)


@pytest.mark.parametrize(
    "title",
    [
        "No notable issues - clean run",
        "Clean ticket, no issues to flag",
        "Nothing to report",
        "ALL GOOD — nothing notable",  # case-insensitive
    ],
)
def test_realistic_noop_titles(title):
    assert is_noop_report(title)


@pytest.mark.parametrize(
    "title",
    [
        "Cut retry tokens",
        "Cap transient retries at 2 in agents/retry.py",
        "Fix tz-naive datetime comparison in refine",
        "Add Trivy scan to docker-publish",
    ],
)
def test_genuine_titles_not_flagged(title):
    assert not is_noop_report(title)
