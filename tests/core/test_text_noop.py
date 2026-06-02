"""Shared no-op detector used by retrospect + report_issue."""

import pytest

from robotsix_mill.core.text_noop import (
    COMPLETION_ANNOUNCEMENT_MARKERS,
    NOOP_MARKERS,
    is_completion_announcement,
    is_noop_report,
)


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


# -- is_completion_announcement tests ----------------------------------------


@pytest.mark.parametrize("marker", COMPLETION_ANNOUNCEMENT_MARKERS)
def test_completion_announcement_each_marker_detected(marker):
    assert is_completion_announcement(f"{marker} — some context")
    assert is_completion_announcement(marker.upper())


@pytest.mark.parametrize(
    "title",
    [
        "spec produced — refine stage complete",
        "Refine complete: cap rebase-agent retries at 3",
        "Refinement complete — returning result",
    ],
)
def test_completion_announcement_realistic_titles(title):
    assert is_completion_announcement(title)


@pytest.mark.parametrize("title", ["", "   ", None])
def test_completion_announcement_empty_is_false(title):
    assert not is_completion_announcement(title)


@pytest.mark.parametrize(
    "title",
    [
        "Cut retry tokens",
        "Fix tz-naive datetime comparison in refine",
        "Add Trivy scan to docker-publish",
        "No notable issues - clean run",  # noop, not completion-announcement
    ],
)
def test_completion_announcement_genuine_titles_not_flagged(title):
    assert not is_completion_announcement(title)
