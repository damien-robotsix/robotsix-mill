"""Tests for robotsix_mill.core.tool_wrappers."""

from __future__ import annotations

import pytest
from pydantic_ai.exceptions import ModelRetry

from robotsix_mill.core.tool_wrappers import (
    wrap_read_tools_with_consecutive_error_guard,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_error_tool(name: str, error_template: str):
    """Return a fake tool callable whose ``__name__`` is *name* and that
    always returns an error string formatted with the *path* kwarg."""

    def tool(*, path: str) -> str:
        return error_template.format(path=path)

    tool.__name__ = name
    return tool


def _make_success_tool(name: str):
    """Return a fake tool callable that always returns a non-error string."""

    def tool(*, path: str) -> str:
        return f"content of {path}"

    tool.__name__ = name
    return tool


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


class TestWrapReadToolsWithConsecutiveErrorGuard:
    """Unit tests for the consecutive-same-error guard."""

    def test_non_error_passes_through(self):
        """Non-error tool results pass through unchanged."""
        tool = _make_success_tool("read_file")
        wrapped = wrap_read_tools_with_consecutive_error_guard([tool])
        assert wrapped[0](path="foo.py") == "content of foo.py"

    def test_non_read_tool_not_wrapped(self):
        """Tools not named read_file or list_dir are passed through as-is."""

        def other_tool() -> str:
            return "error: something"

        other_tool.__name__ = "other"
        wrapped = wrap_read_tools_with_consecutive_error_guard([other_tool])
        # Identity check: should be the same callable, not a wrapper.
        assert wrapped[0] is other_tool

    def test_two_errors_no_retry(self):
        """Two consecutive same-path errors should NOT raise ModelRetry."""
        tool = _make_error_tool(
            "read_file",
            "error: {path!r} does not exist — try list_dir('.')",
        )
        wrapped = wrap_read_tools_with_consecutive_error_guard(
            [tool], max_consecutive=3
        )
        wrapped[0](path="/tmp/x.json")
        result = wrapped[0](path="/tmp/x.json")
        assert isinstance(result, str) and result.startswith("error:")

    def test_three_consecutive_same_path_raises_modelretry(self):
        """Three consecutive same-path same-error-class calls raise ModelRetry."""
        tool = _make_error_tool(
            "read_file",
            "error: {path!r} does not exist — try list_dir('.')",
        )
        wrapped = wrap_read_tools_with_consecutive_error_guard(
            [tool], max_consecutive=3
        )
        wrapped[0](path="/tmp/x.json")
        wrapped[0](path="/tmp/x.json")
        with pytest.raises(ModelRetry) as exc:
            wrapped[0](path="/tmp/x.json")
        assert "/tmp/x.json" in str(exc.value)
        assert "do not retry" in str(exc.value).lower()

    def test_different_path_resets_counter(self):
        """Accessing a different path resets the counter for the previous path."""
        tool = _make_error_tool(
            "read_file",
            "error: {path!r} does not exist — try list_dir('.')",
        )
        wrapped = wrap_read_tools_with_consecutive_error_guard(
            [tool], max_consecutive=3
        )
        wrapped[0](path="/tmp/a.json")  # a=1
        wrapped[0](path="/tmp/a.json")  # a=2
        wrapped[0](path="/tmp/b.json")  # different path → a cleared, b=1
        wrapped[0](path="/tmp/a.json")  # a=1 again (was cleared)
        result = wrapped[0](path="/tmp/a.json")  # a=2
        assert isinstance(result, str) and result.startswith("error:")

    def test_successful_call_resets_counter(self):
        """A successful (non-error) tool call resets all counters."""
        err_tool = _make_error_tool(
            "read_file",
            "error: {path!r} does not exist",
        )
        ok_tool = _make_success_tool("list_dir")
        wrapped = wrap_read_tools_with_consecutive_error_guard(
            [err_tool, ok_tool], max_consecutive=3
        )
        wrapped[0](path="/tmp/x")  # 1
        wrapped[0](path="/tmp/x")  # 2
        wrapped[1](path="/tmp/y")  # success → resets
        result = wrapped[0](path="/tmp/x")  # should be 1 again
        assert isinstance(result, str) and result.startswith("error:")

    def test_different_error_kind_same_path_resets(self):
        """Different error classifications on the same path do not accumulate."""
        call_count = [0]

        def alternating_tool(*, path: str) -> str:
            call_count[0] += 1
            if call_count[0] % 2 == 1:
                return f"error: {path!r} does not exist"
            return f"error: {path!r} is a directory, not a file"

        alternating_tool.__name__ = "read_file"

        wrapped = wrap_read_tools_with_consecutive_error_guard(
            [alternating_tool], max_consecutive=3
        )
        # Sequence: not_exist, is_directory, not_exist, is_directory, not_exist
        for _ in range(5):
            result = wrapped[0](path="/tmp/x")
            # Should never raise because the error kind toggles each call.
            assert isinstance(result, str) and result.startswith("error:")

    def test_list_dir_also_wrapped(self):
        """list_dir tools are wrapped just like read_file."""
        tool = _make_error_tool(
            "list_dir",
            "error: {path!r} does not exist",
        )
        wrapped = wrap_read_tools_with_consecutive_error_guard(
            [tool], max_consecutive=3
        )
        wrapped[0](path="/tmp/d")
        wrapped[0](path="/tmp/d")
        with pytest.raises(ModelRetry):
            wrapped[0](path="/tmp/d")

    def test_read_file_and_list_dir_share_counter(self):
        """Errors from read_file and list_dir on the same path accumulate together."""
        rf = _make_error_tool("read_file", "error: {path!r} does not exist")
        ld = _make_error_tool("list_dir", "error: {path!r} does not exist")
        wrapped = wrap_read_tools_with_consecutive_error_guard(
            [rf, ld], max_consecutive=3
        )
        wrapped[0](path="/tmp/z")  # read_file → 1
        wrapped[1](path="/tmp/z")  # list_dir → 2
        with pytest.raises(ModelRetry):
            wrapped[0](path="/tmp/z")  # read_file → 3 → ModelRetry

    def test_custom_max_consecutive(self):
        """The *max_consecutive* parameter is honoured."""
        tool = _make_error_tool(
            "read_file",
            "error: {path!r} does not exist",
        )
        wrapped = wrap_read_tools_with_consecutive_error_guard(
            [tool], max_consecutive=2
        )
        wrapped[0](path="/tmp/x")
        with pytest.raises(ModelRetry):
            wrapped[0](path="/tmp/x")

    def test_escapes_sandbox_error_kind(self):
        """'escapes the repository' errors are classified distinctly."""
        tool = _make_error_tool(
            "read_file",
            "error: path {path!r} escapes the repository — "
            "all filesystem tools are sandboxed to the repo checkout",
        )
        wrapped = wrap_read_tools_with_consecutive_error_guard(
            [tool], max_consecutive=3
        )
        wrapped[0](path="/etc/passwd")
        wrapped[0](path="/etc/passwd")
        with pytest.raises(ModelRetry):
            wrapped[0](path="/etc/passwd")

    def test_state_reset_after_modelretry(self):
        """After a ModelRetry is raised, state is cleared so a fresh
        sequence can start (for a different path later in the same run)."""
        tool = _make_error_tool(
            "read_file",
            "error: {path!r} does not exist",
        )
        wrapped = wrap_read_tools_with_consecutive_error_guard(
            [tool], max_consecutive=3
        )
        # Trigger ModelRetry on path A
        for _ in range(2):
            wrapped[0](path="/tmp/a")
        with pytest.raises(ModelRetry):
            wrapped[0](path="/tmp/a")
        # Now path B should start fresh — two calls should NOT trigger.
        wrapped[0](path="/tmp/b")  # 1
        result = wrapped[0](path="/tmp/b")  # 2 — still under the limit
        assert isinstance(result, str) and result.startswith("error:")
