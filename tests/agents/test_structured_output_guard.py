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
from robotsix_mill.agents.structured_output_guard import reprompt_if_unstructured


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

    def stub(agent, make_run, *, settings, what, **kw):
        calls.append({"agent": agent, "what": what, "settings": settings})
        return new_result

    return stub, calls


def _install_run_agent_stub(monkeypatch, stub):
    monkeypatch.setattr("robotsix_mill.agents.retry.run_agent", stub)


# ---------------------------------------------------------------------------
# detection-rule branches
# ---------------------------------------------------------------------------


def test_passthrough_when_already_structured(monkeypatch):
    """``result.output`` is already a structured instance → return as-is,
    agent.run_sync is never called a second time."""
    structured = ImplementResult(summary="ok")
    initial = _FakeResult(structured)

    stub, calls = _stub_run_agent_returning(_FakeResult("should-not-be-used"))
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
    assert calls == []  # no re-prompt fired


def test_raw_string_under_threshold_no_reprompt(monkeypatch):
    """A short prose output (< char_threshold) returns the original
    result unchanged — no re-prompt fired."""
    initial = _FakeResult("short prose")

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

    def boom(agent, make_run, *, settings, what, **kw):
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
# Defensive: KeyboardInterrupt is not swallowed by the helper.
# ---------------------------------------------------------------------------


def test_keyboard_interrupt_not_swallowed(monkeypatch):
    """The helper catches ``Exception`` only — base-class signals
    (``KeyboardInterrupt`` / ``SystemExit``) must propagate."""
    initial = _FakeResult("x" * 12_000)

    def interrupt(agent, make_run, *, settings, what, **kw):
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
