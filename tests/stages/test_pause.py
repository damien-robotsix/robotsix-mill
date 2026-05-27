"""Tests for stages.pause helpers.

The headline regression: ``check_for_pause`` previously scanned the
whole serialized history for the ``ask_user`` sentinel, so a resumed
ticket's saved conversation (which still carries the prior turn's
sentinel) re-triggered the pause guard on the NEXT successful run.
The result was a ticket that flipped back to AWAITING_USER_REPLY
immediately after resuming, with no new question being asked.
"""

from __future__ import annotations

import json

from robotsix_mill.stages.pause import (
    _SENTINEL, check_for_pause,
)


def _msg(role: str, parts: list[dict]) -> dict:
    """Minimal serialized pydantic-ai message."""
    return {"role": role, "kind": "request" if role == "request" else "response", "parts": parts}


def _tool_return_part(content: str, tool_call_id: str = "x") -> dict:
    return {
        "part_kind": "tool-return",
        "tool_name": "ask_user",
        "content": content,
        "tool_call_id": tool_call_id,
    }


def _text_part(content: str) -> dict:
    return {"part_kind": "text", "content": content}


def test_no_state_returns_false():
    assert check_for_pause(None) is False
    assert check_for_pause(b"") is False


def test_invalid_json_returns_false():
    assert check_for_pause(b"not json") is False


def test_sentinel_in_last_message_returns_true():
    """Last message contains the sentinel → fresh pause → True."""
    msgs = [
        _msg("response", [_text_part("planning")]),
        _msg("request", [_tool_return_part(_SENTINEL, "ask-1")]),
    ]
    assert check_for_pause(json.dumps(msgs).encode()) is True


def test_sentinel_only_in_earlier_message_returns_false():
    """A sentinel buried in an earlier message (e.g. saved from a
    PRIOR pause that has since been resumed) must NOT re-trigger
    the pause guard. The run is paused only if the LAST tool return
    is the sentinel."""
    msgs = [
        _msg("response", [_text_part("planning")]),
        _msg("request", [_tool_return_part(_SENTINEL, "ask-1")]),
        _msg("request", [{"part_kind": "user-prompt", "content": "operator reply"}]),
        _msg("response", [_text_part("done — here is the result")]),
    ]
    assert check_for_pause(json.dumps(msgs).encode()) is False


def test_non_sentinel_tool_return_in_last_message_returns_false():
    """A non-ask_user tool return in the last message → no pause."""
    msgs = [
        _msg("request", [{
            "part_kind": "tool-return",
            "tool_name": "read_file",
            "content": "file content",
            "tool_call_id": "x",
        }]),
    ]
    assert check_for_pause(json.dumps(msgs).encode()) is False


def test_empty_messages_returns_false():
    assert check_for_pause(b"[]") is False


def test_sentinel_in_both_old_and_new_returns_true():
    """If the run re-paused with ANOTHER ask_user, the last
    message also carries the sentinel — that's a legitimate pause
    and must trigger."""
    msgs = [
        _msg("request", [_tool_return_part(_SENTINEL, "ask-1")]),
        _msg("request", [{"part_kind": "user-prompt", "content": "reply"}]),
        _msg("response", [_text_part("ok one more")]),
        _msg("request", [_tool_return_part(_SENTINEL, "ask-2")]),
    ]
    assert check_for_pause(json.dumps(msgs).encode()) is True
