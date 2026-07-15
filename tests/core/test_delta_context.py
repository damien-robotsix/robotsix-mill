"""Unit tests for robotsix_mill.core.delta_context."""

from robotsix_mill.core.delta_context import (
    trim_draft_for_re_refine,
    trim_spec_for_retry,
)


class TestTrimSpecForRetry:
    """Tests for trim_spec_for_retry."""

    def test_short_spec_returns_unchanged(self):
        """A spec shorter than max_chars is returned verbatim."""
        short = "A brief spec\nwith two lines."
        result = trim_spec_for_retry(short, max_chars=800)
        assert result == short

    def test_long_spec_truncates_at_paragraph_boundary(self):
        """A spec longer than max_chars is truncated at the nearest
        paragraph boundary (double newline) before max_chars."""
        # Build a spec where the first paragraph is ~60 chars and a
        # paragraph break occurs well before max_chars.
        head = "First paragraph.\n\n"
        tail = "Second paragraph. " * 500
        spec = head + tail
        result = trim_spec_for_retry(spec, max_chars=800)
        assert result.startswith(head.rstrip("\n"))
        assert "spec truncated" in result
        assert "you already read the full spec on the first pass" in result

    def test_long_spec_no_paragraph_truncates_at_line_boundary(self):
        """When no paragraph boundary exists before max_chars, the
        function falls back to a line boundary."""
        lines = [f"line {i:04d}" for i in range(200)]
        spec = "\n".join(lines)
        result = trim_spec_for_retry(spec, max_chars=800)
        assert "spec truncated" in result
        # Should have truncated at a line boundary (the last \n before 800).
        # The truncated chars count should be positive.
        omitted_str = result[result.index("spec truncated") :]
        import re

        match = re.search(r"(\d+) chars", omitted_str)
        assert match is not None and int(match.group(1)) > 0

    def test_long_spec_no_newline_truncates_at_max_chars(self):
        """When the spec has no newlines at all, truncation falls back
        to max_chars exactly."""
        spec = "x" * 2000
        result = trim_spec_for_retry(spec, max_chars=800)
        assert result.startswith("x" * 800)
        assert "spec truncated" in result

    def test_custom_max_chars(self):
        """Custom max_chars is respected."""
        spec = "short" + ("\n\n" + "padding\n" * 500)
        result = trim_spec_for_retry(spec, max_chars=200)
        assert len(result) < len(spec)
        assert "spec truncated" in result


class TestTrimDraftForReRefine:
    """Tests for trim_draft_for_re_refine."""

    def test_delegates_to_trim_spec_for_retry(self):
        """trim_draft_for_re_refine produces the same output as
        trim_spec_for_retry for the same input."""
        draft = "Header\n\n" + "body text " * 200
        result = trim_draft_for_re_refine(draft, max_chars=400)
        expected = trim_spec_for_retry(draft, max_chars=400)
        assert result == expected
