"""Unit tests for text_utils (tail-keep, head-tail-keep, and truncate_at_boundary)."""

import pytest

from robotsix_mill.core.text_utils import (
    head_tail_keep,
    tail_keep,
    truncate_at_boundary,
)


def test_under_limit_returns_unchanged():
    text = "line one\nline two\nline three\n"
    assert tail_keep(text, 10_000) == text


def test_exact_limit_returns_unchanged():
    text = "abcde"
    assert tail_keep(text, len(text)) == text


def test_over_limit_keeps_tail_with_note():
    lines = [f"entry {i:03d}" for i in range(500)]
    text = "\n".join(lines) + "\n"
    result = tail_keep(text, 200, label="memory")

    assert result.startswith("[... memory truncated:")
    assert "chars omitted]" in result
    # Most-recent content survives; oldest dropped.
    assert "entry 499" in result
    assert "entry 000" not in result


def test_truncation_at_newline_boundary():
    # 5 lines of 100 'x' chars each, newline-terminated.
    text = "\n".join("x" * 100 for _ in range(5)) + "\n"
    result = tail_keep(text, 150)
    body = result.split("\n\n", 1)[1]
    # Every kept line should be a complete 100-char line.
    for line in body.splitlines():
        assert len(line) == 100


def test_default_label():
    text = "a" * 50 + "\n" + "b" * 50 + "\n"
    result = tail_keep(text, 30)
    assert result.startswith("[... content truncated:")


# --- head_tail_keep (middle truncation) -------------------------------------


def test_head_tail_under_limit_returns_unchanged():
    text = "line one\nline two\nline three\n"
    assert head_tail_keep(text, 10_000) == text


def test_head_tail_zero_cap_returns_unchanged():
    text = "a" * 10_000
    assert head_tail_keep(text, 0) == text


def test_head_tail_over_limit_keeps_head_and_tail_with_marker():
    # Distinct head and tail lines so we can assert both survive.
    head_lines = "\n".join(f"HEAD-{i:04d}" for i in range(2000))
    tail_lines = "\n".join(f"TAIL-{i:04d}" for i in range(2000))
    text = head_lines + "\n" + tail_lines + "\n"

    max_chars = 4000
    result = head_tail_keep(text, max_chars, label="git-diff")

    # Marker line present and labelled.
    assert "[... git-diff truncated:" in result
    assert "omitted from the middle" in result
    # Both early and late content represented.
    assert "HEAD-0000" in result
    assert "TAIL-1999" in result
    # The middle is dropped — some interior lines are gone.
    assert "HEAD-1500" not in result
    # Length bounded by max_chars plus the marker line.
    marker_overhead = len(
        "\n\n[... git-diff truncated: 999999 chars omitted from the middle ...]\n\n"
    )
    assert len(result) <= max_chars + marker_overhead


def test_head_tail_kept_lines_are_complete():
    text = "\n".join("x" * 100 for _ in range(200)) + "\n"
    result = head_tail_keep(text, 1500)
    head_part, rest = result.split("\n\n[... ", 1)
    tail_part = rest.split("...]\n\n", 1)[1]
    for line in head_part.splitlines():
        assert len(line) == 100
    for line in tail_part.splitlines():
        if line:
            assert len(line) == 100


# --- truncate_at_boundary (head-keep with boundary awareness) ----------------


# -- no-op tests --------------------------------------------------------------


def test_truncate_within_limit_returns_unchanged() -> None:
    """Text shorter than max_chars is returned unchanged, no indicator."""
    text = "Hello world. This is a short sentence."
    result = truncate_at_boundary(text, 500)
    assert result == text
    assert "[... description truncated" not in result


def test_truncate_exact_limit_returns_unchanged() -> None:
    """When max_chars == len(text) the text is returned unchanged."""
    text = "exactly forty characters long string!!"
    result = truncate_at_boundary(text, len(text))
    assert result == text
    assert "[... description truncated" not in result


# -- every boundary type ------------------------------------------------------

# Each body has a short prefix ending with the boundary, then 300 'x' padding
# so there is plenty of text to omit.  max_chars=100 guarantees the boundary
# is well inside the scanned prefix.

_BOUNDARY_CASES = [
    # (label, body, max_chars, expected_truncated)
    (". ", "AAA. " + "x" * 300, 100, "AAA."),
    ("! ", "BBB! " + "x" * 300, 100, "BBB!"),
    ("? ", "CCC? " + "x" * 300, 100, "CCC?"),
    (".\\n", "AAA.\n" + "x" * 300, 100, "AAA."),
    ("!\\n", "BBB!\n" + "x" * 300, 100, "BBB!"),
    ("?\\n", "CCC?\n" + "x" * 300, 100, "CCC?"),
    ("\\n\\n", "AAA\n\n" + "x" * 300, 100, "AAA"),
    ("```", "AAA```" + "x" * 300, 100, "AAA```"),
]


@pytest.mark.parametrize(
    "boundary_label,body,max_chars,expected_truncated", _BOUNDARY_CASES
)
def test_truncate_at_each_boundary_type(
    boundary_label: str, body: str, max_chars: int, expected_truncated: str
) -> None:
    """Truncation happens *after* the boundary and the boundary itself is kept."""
    result = truncate_at_boundary(body, max_chars)

    # Must have been truncated (not identical to original).
    assert result != body, f"Expected truncation for boundary {boundary_label!r}"

    # The truncated portion + indicator prefix.
    expected_prefix = expected_truncated + "\n\n[... description truncated;"
    assert result.startswith(expected_prefix), (
        f"Boundary {boundary_label!r}: result does not start with expected prefix.\n"
        f"  result[:80]: {result[:80]!r}\n"
        f"  expected_prefix: {expected_prefix!r}"
    )

    # Omitted count is accurate.
    expected_omitted = len(body) - len(expected_truncated)
    assert f"{expected_omitted} chars omitted]" in result, (
        f"Boundary {boundary_label!r}: missing or wrong omitted count "
        f"(expected {expected_omitted})."
    )


# -- last-boundary-wins tiebreak ----------------------------------------------


def test_truncate_last_boundary_wins_tiebreak() -> None:
    """When multiple boundaries appear, the *rightmost* one is chosen."""
    # ". " at position 3, "\n\n" at position 8 — both in prefix (max_chars=50).
    body = "AAA. BBB\n\n" + "x" * 300
    max_chars = 50
    result = truncate_at_boundary(body, max_chars)

    # If ". " were chosen: truncated = "AAA."  (4 chars)
    # If "\n\n" were chosen: truncated = "AAA. BBB" (9 chars, after rstrip)
    # The rightmost boundary ("\n\n") should win.
    expected_truncated = "AAA. BBB"
    assert result.startswith(expected_truncated + "\n\n[... description truncated;"), (
        f"Expected last boundary (\\n\\n) to win, but got: {result[:80]!r}"
    )

    # Also verify the earlier ". " is NOT where truncation happened.
    # If it had, the body would start with "AAA." not "AAA. BBB".
    assert not result.startswith("AAA.\n\n[... description truncated;")


# -- hard fallback (no boundary found) ----------------------------------------


def test_truncate_hard_fallback_no_boundary_found() -> None:
    """When no boundary exists in the prefix, hard-truncate at max_chars."""
    # A string with no sentence punctuation, no double-newlines, no backticks.
    body = "x" * 500
    max_chars = 100
    result = truncate_at_boundary(body, max_chars)

    # Hard cut at max_chars: the truncated body should be max_chars chars.
    # rstrip() is a no-op on a string of 'x's.
    expected_truncated = "x" * max_chars
    expected_omitted = len(body) - len(expected_truncated)  # 400

    assert result.startswith(expected_truncated + "\n\n[... description truncated;")
    assert f"{expected_omitted} chars omitted]" in result
    # The result should NOT contain the full original body.
    assert body not in result


# -- edge cases ---------------------------------------------------------------


def test_truncate_empty_string_returns_unchanged() -> None:
    """Empty string is always within limit — returned unchanged."""
    result = truncate_at_boundary("", 50)
    assert result == ""
    result = truncate_at_boundary("", 0)
    assert result == ""


def test_truncate_max_chars_zero_non_empty_text() -> None:
    """max_chars=0 triggers hard fallback at position 0 on any non-empty text."""
    text = "Hello world"
    result = truncate_at_boundary(text, 0)

    # Hard fallback at position 0: truncated body is empty string.
    # The indicator should still be present with the full length omitted.
    assert result.startswith("\n\n[... description truncated;")
    assert f"{len(text)} chars omitted]" in result
    # The original text content should not appear before the indicator.
    # (The indicator starts immediately with newlines.)


def test_truncate_unicode_characters() -> None:
    """Multi-byte unicode characters don't confuse boundary scanning."""
    # Emoji + CJK before a ". " boundary, then filler.
    body = "😀🥳日本語. " + "x" * 300
    max_chars = 50
    result = truncate_at_boundary(body, max_chars)

    # The ". " boundary is at code-point position 7.
    # Truncation should keep everything through the ". " boundary.
    expected_truncated = "😀🥳日本語."  # after rstrip
    expected_omitted = len(body) - len(expected_truncated)

    assert result.startswith(expected_truncated + "\n\n[... description truncated;")
    assert f"{expected_omitted} chars omitted]" in result


def test_truncate_omitted_count_accuracy() -> None:
    """The N in '[... description truncated; N chars omitted]' equals
    len(original) - len(truncated_body)."""
    body = "Sentence one. Sentence two. Sentence three. " + "y" * 500
    max_chars = 60
    result = truncate_at_boundary(body, max_chars)

    # Extract the omitted count from the indicator.
    marker = "[... description truncated;"
    assert marker in result
    after_marker = result.split(marker, 1)[1]
    # after_marker looks like " 293 chars omitted]"
    omitted_str = after_marker.strip().split(" ", 1)[0]
    reported_omitted = int(omitted_str)

    # Compute the actual truncated body (everything before the indicator).
    truncated_body = result.split("\n\n[... description truncated;", 1)[0]
    actual_omitted = len(body) - len(truncated_body)

    assert reported_omitted == actual_omitted, (
        f"Reported omitted {reported_omitted} != actual {actual_omitted}"
    )
