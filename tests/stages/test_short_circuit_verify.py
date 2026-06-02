"""Tests for the empty-diff → DONE short-circuit guards."""

from __future__ import annotations

import json

from robotsix_mill.stages import short_circuit_verify as scv


def _msgs(*tool_names: str) -> bytes:
    """Build a ``new_messages_json()``-shaped payload invoking *tool_names*.

    Each name becomes a ``tool-call`` part on a single model-request message,
    interleaved with a benign text part and a ``tool-return`` part so the
    scanner has to ignore non-tool-call parts.
    """
    parts: list[dict] = [{"part_kind": "text", "content": "thinking..."}]
    for name in tool_names:
        parts.append(
            {
                "part_kind": "tool-call",
                "tool_name": name,
                "args": {"path": "x.py"},
                "tool_call_id": f"call_{name}",
            }
        )
        parts.append(
            {
                "part_kind": "tool-return",
                "tool_name": name,
                "content": "ok",
            }
        )
    return json.dumps([{"parts": parts}]).encode()


# --- run_invoked_edit_tools -------------------------------------------------


def test_detects_mill_edit_tools():
    found = scv.run_invoked_edit_tools(_msgs("write_file", "edit_file", "delete_file"))
    assert sorted(found) == ["delete_file", "edit_file", "write_file"]


def test_detects_claude_sdk_edit_tools():
    found = scv.run_invoked_edit_tools(
        _msgs("Write", "Edit", "MultiEdit", "NotebookEdit")
    )
    assert sorted(found) == ["Edit", "MultiEdit", "NotebookEdit", "Write"]


def test_command_and_read_tools_are_not_edit_claims():
    # run_command / Bash / read tools read as often as they write — they must
    # NOT count as an edit claim or every test-running no-change run blocks.
    assert scv.run_invoked_edit_tools(_msgs("run_command", "Bash", "read_file")) == []


def test_accepts_str_payload_not_only_bytes():
    payload = _msgs("write_file").decode()
    assert scv.run_invoked_edit_tools(payload) == ["write_file"]


def test_none_and_empty_yield_empty():
    assert scv.run_invoked_edit_tools(None) == []
    assert scv.run_invoked_edit_tools(b"") == []
    assert scv.run_invoked_edit_tools("") == []


def test_malformed_json_fails_open():
    # A parse error must never manufacture a contradiction (would wrongly
    # BLOCK a good run) — fail open to "no edits".
    assert scv.run_invoked_edit_tools(b"{not json") == []
    assert scv.run_invoked_edit_tools(b'{"parts": 3}') == []
    assert scv.run_invoked_edit_tools(b'"a string, not a list"') == []


def test_ignores_malformed_parts():
    payload = json.dumps(
        [{"parts": ["not-a-dict", {"part_kind": "tool-call"}]}, "not-a-dict"]
    ).encode()
    assert scv.run_invoked_edit_tools(payload) == []


# --- detect_edit_claim_contradiction ----------------------------------------


def test_contradiction_when_edits_invoked_but_no_diff():
    found = scv.detect_edit_claim_contradiction(
        has_changes=False, new_messages=_msgs("write_file", "write_file", "Edit")
    )
    # de-duplicated + sorted
    assert found == ["Edit", "write_file"]


def test_no_contradiction_when_diff_present():
    # A real diff means no short-circuit is happening — nothing to verify.
    assert (
        scv.detect_edit_claim_contradiction(
            has_changes=True, new_messages=_msgs("write_file")
        )
        == []
    )


def test_no_contradiction_for_genuine_no_change_run():
    # Empty diff + only reads/commands == legitimate no-change.
    assert (
        scv.detect_edit_claim_contradiction(
            has_changes=False, new_messages=_msgs("run_command", "read_file")
        )
        == []
    )


def test_no_contradiction_when_no_tool_calls():
    assert (
        scv.detect_edit_claim_contradiction(has_changes=False, new_messages=None) == []
    )
