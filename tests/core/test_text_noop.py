"""Shared no-op detector used by retrospect + report_issue."""

import pytest

from robotsix_mill.core.text_noop import (
    CODE_CHANGE_MANDATE_PHRASES,
    COMPLETION_ANNOUNCEMENT_MARKERS,
    NOOP_MARKERS,
    PLACEHOLDER_BODY_PHRASES,
    degenerate_body_reason,
    is_completion_announcement,
    is_degenerate_body,
    is_noop_report,
    spec_demands_code_change,
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


# -- degenerate_body_reason / is_degenerate_body ----------------------------


@pytest.mark.parametrize("text", ["", "   ", None])
def test_degenerate_empty(text):
    assert is_degenerate_body(text)
    assert degenerate_body_reason(text) == "body is empty"


def test_degenerate_punctuation_only():
    assert is_degenerate_body("...")
    assert degenerate_body_reason("...") == "body is only punctuation"
    assert is_degenerate_body("!@#$%")
    assert degenerate_body_reason("!@#$%") == "body is only punctuation"


@pytest.mark.parametrize(
    "text",
    [
        "tbd",
        "TBD",
        "todo",
        "see above",
        "See the spec above.",
        "refer to spec",
        "as above",
    ],
)
def test_degenerate_placeholder_phrases(text):
    assert is_degenerate_body(text)
    reason = degenerate_body_reason(text)
    assert reason is not None
    assert "placeholder phrase" in reason


def test_non_degenerate_prescriptive_draft():
    """A prescriptive draft with git mv commands is NOT degenerate."""
    draft = (
        "Reorganize module core:\n\n"
        "```bash\ngit mv tests/test_foo.py tests/core/test_foo.py\n"
        "git mv tests/test_bar.py tests/core/test_bar.py\n```\n\n"
        "Update `docs/modules.yaml` globs from `tests/test_*.py` "
        "to `tests/core/test_*.py`."
    )
    assert not is_degenerate_body(draft)
    assert degenerate_body_reason(draft) is None


def test_non_degenerate_draft_over_120_chars():
    """Any body over 120 chars is never degenerate, even with placeholder words."""
    body = "see above " * 15  # >120 chars, contains "see above"
    assert not is_degenerate_body(body)
    assert degenerate_body_reason(body) is None


def test_degenerate_short_placeholder():
    """A short body with 'tbd' IS degenerate."""
    assert is_degenerate_body("tbd")
    reason = degenerate_body_reason("tbd")
    assert "tbd" in reason


def test_degenerate_short_normalized_matches():
    """Normalisation catches punctuation-separated placeholders."""
    assert is_degenerate_body("see-above")
    reason = degenerate_body_reason("see-above")
    assert reason is not None
    assert "placeholder phrase" in reason


def test_every_placeholder_phrase_is_tested():
    """Smoke-test: every PLACEHOLDER_BODY_PHRASES entry is caught."""
    for phrase in PLACEHOLDER_BODY_PHRASES:
        assert is_degenerate_body(phrase), f"phrase {phrase!r} not caught"
        reason = degenerate_body_reason(phrase)
        assert reason is not None
        assert phrase in reason


# -- spec_demands_code_change ------------------------------------------------


@pytest.mark.parametrize("phrase", CODE_CHANGE_MANDATE_PHRASES)
def test_each_code_change_mandate_phrase_detected(phrase):
    """Every CODE_CHANGE_MANDATE_PHRASES entry is detected."""
    assert spec_demands_code_change(
        f"## Acceptance criteria\n\nThe implementation {phrase}."
    ), f"phrase {phrase!r} not detected"


@pytest.mark.parametrize(
    "text",
    [
        "The agent must investigate the failure.",
        "Document the findings in a comment.",
        "This ticket is information-only.",
        "No code changes are required.",
        "The existing logic already handles this.",
        "Remove dead code from helpers.py.",
    ],
)
def test_non_mandate_text_not_flagged(text):
    """Text without a mandate phrase must NOT be flagged."""
    assert not spec_demands_code_change(text), f"text {text!r} wrongly flagged"


def test_spec_demands_code_change_empty_or_none():
    assert not spec_demands_code_change("")
    assert not spec_demands_code_change(None)
