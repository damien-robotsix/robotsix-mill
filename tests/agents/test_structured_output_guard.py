"""Unit tests for the shared post-hoc structured-output guard.

Covers detection rule branches (already-structured passthrough, under
char threshold, tool-call gate honoured when required, re-prompt fires,
re-prompt failure returns original result) using a stub ``run_agent``
seam. No real agent is instantiated.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from robotsix_mill.agents.coordinating import ImplementResult
from robotsix_mill.agents.structured_output_guard import (
    reprompt_if_unstructured,
    _has_tool_calls,
    _zero_tool_call_reprompt_needed,
)


class _FakeResult:
    """Stand-in for ``AgentRunResult`` — only ``.output`` and
    ``.all_messages()`` are read by the guard."""

    def __init__(self, output, messages=None):
        self.output = output
        self._messages = messages or []

    def all_messages(self):
        return list(self._messages)


def _msg_with_parts(*part_kinds):
    parts = [SimpleNamespace(part_kind=k) for k in part_kinds]
    return SimpleNamespace(parts=parts)


def _stub_run_agent_returning(new_result):
    """Build a stub ``run_agent`` that records calls and returns
    *new_result* (always — first or subsequent invocation)."""
    calls: list[dict] = []

    def stub(agent, make_run, *, what, **kw):
        calls.append({"agent": agent, "what": what})
        return new_result

    return stub, calls


def _install_run_agent_stub(monkeypatch, stub):
    monkeypatch.setattr("robotsix_mill.agents.retry.run_agent", stub)


# ---------------------------------------------------------------------------
# _has_tool_calls / _zero_tool_call_reprompt_needed unit tests
# ---------------------------------------------------------------------------


def test_has_tool_calls_true_when_tool_call_present():
    r = _FakeResult("x", messages=[_msg_with_parts("tool-call")])
    assert _has_tool_calls(r) is True


def test_has_tool_calls_true_when_tool_return_present():
    r = _FakeResult("x", messages=[_msg_with_parts("tool-return")])
    assert _has_tool_calls(r) is True


def test_has_tool_calls_false_when_no_tool_parts():
    r = _FakeResult("x", messages=[_msg_with_parts("text")])
    assert _has_tool_calls(r) is False


def test_has_tool_calls_false_when_no_messages():
    r = _FakeResult("x")
    assert _has_tool_calls(r) is False


def test_zero_tool_call_reprompt_true_for_unstructured_no_tools():
    r = _FakeResult("x" * 100, messages=[_msg_with_parts("text")])
    assert _zero_tool_call_reprompt_needed(r, ImplementResult, "test") is True


def test_zero_tool_call_reprompt_false_when_tool_calls_present():
    r = _FakeResult("x", messages=[_msg_with_parts("tool-call")])
    assert _zero_tool_call_reprompt_needed(r, ImplementResult, "test") is False


def test_zero_tool_call_reprompt_false_for_no_change_needed_structured():
    r = _FakeResult(
        ImplementResult(summary="ok", no_change_needed=True),
        messages=[_msg_with_parts("text")],
    )
    assert _zero_tool_call_reprompt_needed(r, ImplementResult, "test") is False


def test_zero_tool_call_reprompt_true_for_structured_without_no_change():
    r = _FakeResult(
        ImplementResult(summary="ok"),
        messages=[_msg_with_parts("text")],
    )
    assert _zero_tool_call_reprompt_needed(r, ImplementResult, "test") is True


# ---------------------------------------------------------------------------
# detection-rule branches
# ---------------------------------------------------------------------------


def test_passthrough_when_structured_and_tool_calls_present(monkeypatch):
    """Structured output WITH tool calls → return as-is.
    (The zero-tool-call gate sees tool calls, falls through to the
    structured check which passes.)"""
    structured = ImplementResult(summary="ok")
    initial = _FakeResult(
        structured,
        messages=[_msg_with_parts("tool-call")],
    )

    stub, calls = _stub_run_agent_returning(_FakeResult("unused"))
    _install_run_agent_stub(monkeypatch, stub)

    out = reprompt_if_unstructured(
        result=initial,
        agent=object(),
        expected_type=ImplementResult,
        reprompt_message="ignored",
        settings=object(),
        what="implement",
        require_no_tool_calls=True,
    )
    assert out is initial
    assert calls == []


def test_raw_string_under_threshold_no_reprompt(monkeypatch):
    """A short prose output (< char_threshold) WITH tool calls returns
    the original result unchanged. When require_no_tool_calls=True but
    tool calls ARE present, the zero-tool-call gate does not fire, and
    the short-prose check below catches it."""
    initial = _FakeResult(
        "short prose",
        messages=[_msg_with_parts("tool-call")],
    )

    stub, calls = _stub_run_agent_returning(_FakeResult("unused"))
    _install_run_agent_stub(monkeypatch, stub)

    out = reprompt_if_unstructured(
        result=initial,
        agent=object(),
        expected_type=ImplementResult,
        reprompt_message="ignored",
        settings=object(),
        what="implement",
        require_no_tool_calls=True,
    )
    assert out is initial
    assert calls == []


def test_structured_no_change_needed_carve_out(monkeypatch):
    """Structured output with ``no_change_needed=True`` and zero tool
    calls passes through — the agent correctly determined the spec is
    already satisfied."""
    structured = ImplementResult(summary="ok", no_change_needed=True)
    initial = _FakeResult(structured)

    stub, calls = _stub_run_agent_returning(_FakeResult("unused"))
    _install_run_agent_stub(monkeypatch, stub)

    out = reprompt_if_unstructured(
        result=initial,
        agent=object(),
        expected_type=ImplementResult,
        reprompt_message="ignored",
        settings=object(),
        what="implement",
        require_no_tool_calls=True,
    )
    assert out is initial
    assert calls == []


def test_structured_zero_tool_calls_no_change_false_reprompts(monkeypatch):
    """Structured output with zero tool calls AND
    ``no_change_needed=False`` → the agent returned a clean envelope
    without doing any work. Re-prompt fires."""
    structured = ImplementResult(summary="ok")
    initial = _FakeResult(structured)
    successor = _FakeResult(ImplementResult(summary="after retry"))

    stub, calls = _stub_run_agent_returning(successor)
    _install_run_agent_stub(monkeypatch, stub)

    out = reprompt_if_unstructured(
        result=initial,
        agent=object(),
        expected_type=ImplementResult,
        reprompt_message="please use tools",
        settings=object(),
        what="implement",
        require_no_tool_calls=True,
    )
    assert out is successor
    assert isinstance(out.output, ImplementResult)
    assert len(calls) == 1


def test_zero_tool_calls_reprompts_even_under_char_threshold(monkeypatch):
    """When ``require_no_tool_calls=True``, a short prose output with
    zero tool calls still triggers a re-prompt — the zero-tool-call
    gate fires before the char-threshold check."""
    initial = _FakeResult("short prose")
    successor = _FakeResult(ImplementResult(summary="after retry"))

    stub, calls = _stub_run_agent_returning(successor)
    _install_run_agent_stub(monkeypatch, stub)

    out = reprompt_if_unstructured(
        result=initial,
        agent=object(),
        expected_type=ImplementResult,
        reprompt_message="please structure",
        settings=object(),
        what="implement",
        require_no_tool_calls=True,
    )
    assert out is successor
    assert isinstance(out.output, ImplementResult)
    assert len(calls) == 1


def test_short_prose_zero_tool_calls_triggers_reprompt(monkeypatch):
    """Even short prose with zero tool calls triggers a re-prompt when
    require_no_tool_calls=True — the zero-tool-call gate runs BEFORE
    the char_threshold check."""
    initial = _FakeResult("short", messages=[_msg_with_parts("text")])
    successor = _FakeResult(ImplementResult(summary="after retry"))

    stub, calls = _stub_run_agent_returning(successor)
    _install_run_agent_stub(monkeypatch, stub)

    out = reprompt_if_unstructured(
        result=initial,
        agent=object(),
        expected_type=ImplementResult,
        reprompt_message="please structure",
        settings=object(),
        what="implement",
        require_no_tool_calls=True,
    )
    assert out is successor
    assert len(calls) == 1


def test_raw_string_over_threshold_reprompts_once_and_succeeds(monkeypatch):
    """Over-threshold prose with no tool calls (require_no_tool_calls=True)
    → re-prompt fires, structured result is returned, run_agent called
    exactly once on the re-prompt path."""
    initial = _FakeResult("x" * 12_000, messages=[_msg_with_parts("text")])
    successor = _FakeResult(ImplementResult(summary="after retry"))

    stub, calls = _stub_run_agent_returning(successor)
    _install_run_agent_stub(monkeypatch, stub)

    out = reprompt_if_unstructured(
        result=initial,
        agent=object(),
        expected_type=ImplementResult,
        reprompt_message="please structure",
        settings=object(),
        what="implement",
        require_no_tool_calls=True,
    )
    assert out is successor
    assert isinstance(out.output, ImplementResult)
    assert len(calls) == 1


def test_raw_string_over_threshold_with_tool_calls_skipped_when_required(monkeypatch):
    """Implement-specific gate: a ``tool-call`` part in the history
    means the model DID use tools — skip the re-prompt even though the
    output is prose-only over threshold."""
    initial = _FakeResult(
        "x" * 12_000,
        messages=[_msg_with_parts("tool-call")],
    )
    stub, calls = _stub_run_agent_returning(_FakeResult("unused"))
    _install_run_agent_stub(monkeypatch, stub)

    out = reprompt_if_unstructured(
        result=initial,
        agent=object(),
        expected_type=ImplementResult,
        reprompt_message="please structure",
        settings=object(),
        what="implement",
        require_no_tool_calls=True,
    )
    assert out is initial
    assert calls == []


def test_reprompt_failure_returns_original_result(monkeypatch, caplog):
    """When the re-prompt raises, the original result is returned (not
    raised) and a warning is logged."""
    initial = _FakeResult("x" * 12_000)

    def boom(agent, make_run, *, what, **kw):
        raise RuntimeError("model unavailable")

    _install_run_agent_stub(monkeypatch, boom)

    caplog.set_level(
        logging.WARNING, logger="robotsix_mill.agents.structured_output_guard"
    )
    out = reprompt_if_unstructured(
        result=initial,
        agent=object(),
        expected_type=ImplementResult,
        reprompt_message="please structure",
        settings=object(),
        what="review (re-prompt after prose-only)",
        require_no_tool_calls=False,
    )
    assert out is initial
    assert any("re-prompt" in rec.message for rec in caplog.records)


def test_no_tool_calls_check_disabled_for_review_path(monkeypatch):
    """When ``require_no_tool_calls=False`` the re-prompt fires regardless
    of whether tool-call parts are present in the message history."""
    initial = _FakeResult(
        "x" * 12_000,
        messages=[_msg_with_parts("tool-call", "tool-return")],
    )
    successor = _FakeResult(ImplementResult(summary="after retry"))

    stub, calls = _stub_run_agent_returning(successor)
    _install_run_agent_stub(monkeypatch, stub)

    out = reprompt_if_unstructured(
        result=initial,
        agent=object(),
        expected_type=ImplementResult,
        reprompt_message="please structure",
        settings=object(),
        what="review (re-prompt after prose-only)",
        require_no_tool_calls=False,
    )
    assert out is successor
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Zero-tool-call gate: structured output, no tool calls
# ---------------------------------------------------------------------------


def test_structured_zero_tool_calls_triggers_reprompt(monkeypatch):
    """A structured ImplementResult produced with zero tool calls MUST
    trigger a re-prompt — this is the exact gap the guard fixes."""
    initial = _FakeResult(
        ImplementResult(summary="ok"),
        messages=[_msg_with_parts("text")],
    )
    successor = _FakeResult(ImplementResult(summary="after retry"))

    stub, calls = _stub_run_agent_returning(successor)
    _install_run_agent_stub(monkeypatch, stub)

    out = reprompt_if_unstructured(
        result=initial,
        agent=object(),
        expected_type=ImplementResult,
        reprompt_message="use tools!",
        settings=object(),
        what="implement",
        require_no_tool_calls=True,
    )
    assert out is successor
    assert len(calls) == 1


def test_structured_no_change_needed_passes_through(monkeypatch):
    """A structured ImplementResult with no_change_needed=True and zero
    tool calls is a deliberate signal — pass through without re-prompt."""
    initial = _FakeResult(
        ImplementResult(summary="already done", no_change_needed=True),
        messages=[_msg_with_parts("text")],
    )

    stub, calls = _stub_run_agent_returning(_FakeResult("unused"))
    _install_run_agent_stub(monkeypatch, stub)

    out = reprompt_if_unstructured(
        result=initial,
        agent=object(),
        expected_type=ImplementResult,
        reprompt_message="ignored",
        settings=object(),
        what="implement",
        require_no_tool_calls=True,
    )
    assert out is initial
    assert calls == []


def test_zero_tool_call_reprompt_failure_returns_original(monkeypatch, caplog):
    """When the zero-tool-call re-prompt raises, the original result is
    returned and a warning is logged."""
    initial = _FakeResult(
        ImplementResult(summary="ok"),
        messages=[_msg_with_parts("text")],
    )

    def boom(agent, make_run, *, what, **kw):
        raise RuntimeError("model unavailable")

    _install_run_agent_stub(monkeypatch, boom)

    caplog.set_level(
        logging.WARNING, logger="robotsix_mill.agents.structured_output_guard"
    )
    out = reprompt_if_unstructured(
        result=initial,
        agent=object(),
        expected_type=ImplementResult,
        reprompt_message="use tools!",
        settings=object(),
        what="implement",
        require_no_tool_calls=True,
    )
    assert out is initial
    assert any("zero-tool-call" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Defensive: KeyboardInterrupt is not swallowed by the helper.
# ---------------------------------------------------------------------------


def test_keyboard_interrupt_not_swallowed(monkeypatch):
    """The helper catches ``Exception`` only — base-class signals
    (``KeyboardInterrupt`` / ``SystemExit``) must propagate."""
    initial = _FakeResult("x" * 12_000)

    def interrupt(agent, make_run, *, what, **kw):
        raise KeyboardInterrupt

    _install_run_agent_stub(monkeypatch, interrupt)
    with pytest.raises(KeyboardInterrupt):
        reprompt_if_unstructured(
            result=initial,
            agent=object(),
            expected_type=ImplementResult,
            reprompt_message="please structure",
            settings=object(),
            what="implement",
            require_no_tool_calls=False,
        )
