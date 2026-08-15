"""Unit tests for robotsix_mill.core.delta_context."""

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from robotsix_mill.core.delta_context import (
    compact_message_history,
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
        assert match is not None
        assert int(match.group(1)) > 0

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


class TestCompactMessageHistory:
    """Tests for compact_message_history."""

    @staticmethod
    def _history(n_turns: int) -> list:
        msgs = [ModelRequest(parts=[UserPromptPart(content="initial prompt")])]
        for i in range(n_turns):
            msgs.append(
                ModelResponse(
                    parts=[
                        ToolCallPart(
                            tool_name="read_file",
                            args={"path": f"f{i}.py"},
                            tool_call_id=f"call_{i}",
                        )
                    ]
                )
            )
            msgs.append(
                ModelRequest(
                    parts=[
                        ToolReturnPart(
                            tool_name="read_file",
                            content=f"content {i}",
                            tool_call_id=f"call_{i}",
                        )
                    ]
                )
            )
        msgs.append(ModelResponse(parts=[TextPart(content="done")]))
        return msgs

    def test_under_cap_returns_unchanged(self):
        history = self._history(3)
        result = compact_message_history(history, max_turns=5)
        assert result == history

    def test_compacts_to_last_n_turns(self):
        history = self._history(10)
        result = compact_message_history(history, max_turns=3, summary_max_chars=10000)
        # First message is the synthetic rolling-summary user prompt.
        assert isinstance(result[0], ModelRequest)
        summary = result[0].parts[0]
        assert isinstance(summary, UserPromptPart)
        assert "summarized" in summary.content
        # The kept suffix begins at an assistant tool-call turn.
        assert isinstance(result[1], ModelResponse)
        # Exactly the last 3 tool calls survive, newest first dropped is gone.
        kept_calls = [
            p
            for m in result
            if isinstance(m, ModelResponse)
            for p in m.parts
            if isinstance(p, ToolCallPart)
        ]
        assert [p.tool_call_id for p in kept_calls] == ["call_7", "call_8", "call_9"]

    def test_no_orphaned_tool_returns(self):
        history = self._history(10)
        result = compact_message_history(history, max_turns=3, summary_max_chars=10000)
        call_ids = {
            p.tool_call_id
            for m in result
            if isinstance(m, ModelResponse)
            for p in m.parts
            if isinstance(p, ToolCallPart)
        }
        for m in result:
            if isinstance(m, ModelRequest):
                for p in m.parts:
                    if isinstance(p, ToolReturnPart):
                        assert p.tool_call_id in call_ids

    def test_disabled_returns_unchanged(self):
        history = self._history(10)
        assert compact_message_history(history, max_turns=0) is history

    def test_no_tool_turns_returns_unchanged(self):
        history = [
            ModelRequest(parts=[UserPromptPart(content="hi")]),
            ModelResponse(parts=[TextPart(content="hello")]),
        ]
        assert compact_message_history(history, max_turns=2) is history

    def test_summary_is_bounded(self):
        history = self._history(10)
        result = compact_message_history(history, max_turns=3, summary_max_chars=200)
        summary = result[0].parts[0]
        # The summary is a bounded rolling digest — far smaller than the
        # raw dropped prefix and within a small slack of the configured cap.
        assert len(summary.content) < 600
