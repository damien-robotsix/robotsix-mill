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


# --- run_claimed_edited_paths / detect_missing_claimed_files ----------------


def _path_msgs(*calls: tuple[str, str, str]) -> bytes:
    """Build a ``new_messages_json()``-shaped payload of edit/read tool-calls.

    Each *call* is ``(tool_name, path_key, path_value)`` where *path_key* is
    ``"path"`` (mill fs tools) or ``"file_path"`` (Claude SDK editors). The
    call becomes a ``tool-call`` part interleaved with a text + tool-return
    part so the scanner has to skip non-tool-call parts.
    """
    parts: list[dict] = [{"part_kind": "text", "content": "thinking..."}]
    for tool_name, path_key, path_value in calls:
        parts.append(
            {
                "part_kind": "tool-call",
                "tool_name": tool_name,
                "args": {path_key: path_value},
                "tool_call_id": f"call_{tool_name}",
            }
        )
        parts.append(
            {
                "part_kind": "tool-return",
                "tool_name": tool_name,
                "content": "ok",
            }
        )
    return json.dumps([{"parts": parts}]).encode()


def test_claimed_paths_extracts_mill_and_sdk_basenames():
    found = scv.run_claimed_edited_paths(
        _path_msgs(
            ("write_file", "path", "src/robotsix_mill/runtime/static/board.js"),
            ("Edit", "file_path", "/abs/repo/src/app.py"),
        )
    )
    assert sorted(found) == ["app.py", "board.js"]


def test_claimed_paths_dedupes_basenames():
    found = scv.run_claimed_edited_paths(
        _path_msgs(
            ("write_file", "path", "a/board.js"),
            ("edit_file", "path", "b/board.js"),
        )
    )
    assert found == ["board.js"]


def test_claimed_paths_ignores_non_edit_tools():
    found = scv.run_claimed_edited_paths(
        _path_msgs(
            ("run_command", "path", "scripts/run.sh"),
            ("Bash", "path", "scripts/x.sh"),
            ("read_file", "path", "src/app.py"),
        )
    )
    assert found == []


def test_claimed_paths_fails_open():
    assert scv.run_claimed_edited_paths(None) == []
    assert scv.run_claimed_edited_paths(b"") == []
    assert scv.run_claimed_edited_paths("") == []
    assert scv.run_claimed_edited_paths(b"{not json") == []
    assert scv.run_claimed_edited_paths(b'"a string, not a list"') == []
    # missing args / non-string path keys → skip the entry.
    payload = json.dumps(
        [
            {
                "parts": [
                    {"part_kind": "tool-call", "tool_name": "write_file"},
                    {
                        "part_kind": "tool-call",
                        "tool_name": "edit_file",
                        "args": {"path": 3},
                    },
                ]
            }
        ]
    ).encode()
    assert scv.run_claimed_edited_paths(payload) == []


def test_missing_when_claimed_and_named_but_absent():
    # board.js was edited (tool-call) and named in the summary, but is NOT in
    # the net diff → contradiction.
    missing = scv.detect_missing_claimed_files(
        changed_files=["src/app.py"],
        new_messages=_path_msgs(("write_file", "path", "static/board.js")),
        summary="Applied the openCandidates() guard fix in board.js.",
    )
    assert missing == ["board.js"]


def test_no_missing_when_claimed_file_landed():
    missing = scv.detect_missing_claimed_files(
        changed_files=["src/robotsix_mill/runtime/static/board.js"],
        new_messages=_path_msgs(("write_file", "path", "static/board.js")),
        summary="Applied the openCandidates() guard fix in board.js.",
    )
    assert missing == []


def test_no_missing_when_edited_but_not_named_in_summary():
    # Edit-then-revert false-positive guard: the file was targeted by an edit
    # tool-call but the summary does not name it as a landed fix, so it must
    # not be flagged.
    missing = scv.detect_missing_claimed_files(
        changed_files=["src/app.py"],
        new_messages=_path_msgs(("write_file", "path", "static/board.js")),
        summary="Reworked app.py only.",
    )
    assert missing == []


def test_no_missing_when_summary_falsy():
    msgs = _path_msgs(("write_file", "path", "static/board.js"))
    assert (
        scv.detect_missing_claimed_files(
            changed_files=["src/app.py"], new_messages=msgs, summary=None
        )
        == []
    )
    assert (
        scv.detect_missing_claimed_files(
            changed_files=["src/app.py"], new_messages=msgs, summary=""
        )
        == []
    )


def test_no_missing_when_only_read_or_command_tools():
    missing = scv.detect_missing_claimed_files(
        changed_files=["src/app.py"],
        new_messages=_path_msgs(
            ("read_file", "path", "static/board.js"),
            ("run_command", "path", "scripts/run.sh"),
        ),
        summary="Inspected board.js and run.sh but made no edits.",
    )
    assert missing == []


def test_missing_output_deduped_and_sorted():
    missing = scv.detect_missing_claimed_files(
        changed_files=["src/app.py"],
        new_messages=_path_msgs(
            ("write_file", "path", "x/zeta.py"),
            ("edit_file", "path", "y/alpha.py"),
            ("Edit", "file_path", "/abs/z/alpha.py"),
        ),
        summary="Edited zeta.py and alpha.py as required.",
    )
    assert missing == ["alpha.py", "zeta.py"]


# --- run_claimed_edited_rawpaths ---------------------------------------------


def _msgs_with_paths(*specs: tuple[str, dict]) -> bytes:
    """Payload with one tool-call part per (tool_name, args) spec."""
    parts: list[dict] = []
    for name, args in specs:
        parts.append(
            {
                "part_kind": "tool-call",
                "tool_name": name,
                "args": args,
                "tool_call_id": f"call_{name}_{len(parts)}",
            }
        )
    return json.dumps([{"parts": parts}]).encode()


def test_rawpaths_keeps_full_relative_path():
    payload = _msgs_with_paths(("write_file", {"path": "src/pkg/msg/Status.msg"}))
    assert scv.run_claimed_edited_rawpaths(payload) == ["src/pkg/msg/Status.msg"]


def test_rawpaths_keeps_absolute_claude_sdk_path():
    payload = _msgs_with_paths(("Write", {"file_path": "/ws/repo/src/a.py"}))
    assert scv.run_claimed_edited_rawpaths(payload) == ["/ws/repo/src/a.py"]


def test_rawpaths_dedup_and_order():
    payload = _msgs_with_paths(
        ("write_file", {"path": "a/b.py"}),
        ("edit_file", {"path": "a/b.py"}),
        ("write_file", {"path": "c/d.py"}),
    )
    assert scv.run_claimed_edited_rawpaths(payload) == ["a/b.py", "c/d.py"]


def test_rawpaths_ignores_non_edit_tools():
    payload = _msgs_with_paths(("read_file", {"path": "a.py"}))
    assert scv.run_claimed_edited_rawpaths(payload) == []


def test_rawpaths_fail_open_on_malformed_json():
    assert scv.run_claimed_edited_rawpaths(b"{not json") == []
    assert scv.run_claimed_edited_rawpaths(None) == []


# --- extract_replayable_edits ----------------------------------------------


def test_extract_replayable_edits_mill_tools():
    payload = _msgs_with_paths(
        ("edit_file", {"path": "a.py", "old_string": "x", "new_string": "y"}),
        ("write_file", {"path": "b.py", "content": "hi"}),
        ("delete_file", {"path": "c.py"}),
    )
    assert scv.extract_replayable_edits(payload) == [
        {"kind": "edit", "path": "a.py", "old": "x", "new": "y"},
        {"kind": "write", "path": "b.py", "content": "hi"},
        {"kind": "delete", "path": "c.py"},
    ]


def test_extract_replayable_edits_claude_sdk_tools():
    payload = _msgs_with_paths(
        ("Edit", {"file_path": "/clone/a.py", "old_string": "x", "new_string": "y"}),
        ("Write", {"file_path": "/clone/b.py", "content": "hi"}),
    )
    assert scv.extract_replayable_edits(payload) == [
        {"kind": "edit", "path": "/clone/a.py", "old": "x", "new": "y"},
        {"kind": "write", "path": "/clone/b.py", "content": "hi"},
    ]


def test_extract_replayable_edits_unreplayable_kind_fails_closed():
    # MultiEdit cannot be faithfully reconstructed → None (caller BLOCKs).
    payload = _msgs_with_paths(
        ("edit_file", {"path": "a.py", "old_string": "x", "new_string": "y"}),
        ("MultiEdit", {"file_path": "a.py", "edits": [{"old": "1", "new": "2"}]}),
    )
    assert scv.extract_replayable_edits(payload) is None


def test_extract_replayable_edits_missing_args_fails_closed():
    # An edit_file without old_string can't be replayed → None.
    payload = _msgs_with_paths(("edit_file", {"path": "a.py", "new_string": "y"}))
    assert scv.extract_replayable_edits(payload) is None


def test_extract_replayable_edits_args_as_json_string():
    # pydantic-ai sometimes encodes args as a JSON string; both are accepted.
    parts = [
        {
            "part_kind": "tool-call",
            "tool_name": "edit_file",
            "args": json.dumps({"path": "a.py", "old_string": "x", "new_string": "y"}),
            "tool_call_id": "c1",
        }
    ]
    payload = json.dumps([{"parts": parts}]).encode()
    assert scv.extract_replayable_edits(payload) == [
        {"kind": "edit", "path": "a.py", "old": "x", "new": "y"}
    ]


def test_extract_replayable_edits_no_edits_is_empty_list():
    payload = _msgs_with_paths(("read_file", {"path": "a.py"}))
    assert scv.extract_replayable_edits(payload) == []


def test_extract_replayable_edits_malformed_fails_closed():
    assert scv.extract_replayable_edits(b"{not json") is None
    assert scv.extract_replayable_edits(None) == []


# --- analyze_pass_progress ---------------------------------------------------


def _tool_msgs(*tool_names: str) -> bytes:
    """Build a ``new_messages_json()``-shaped payload of *tool_names*.

    Each name becomes its own message (one tool-call part per message)
    so the order is unambiguous.  Non-tool-call parts are interleaved
    to verify the scanner skips them.
    """
    messages: list[dict] = []
    for name in tool_names:
        messages.append(
            {
                "parts": [
                    {"part_kind": "text", "content": "thinking..."},
                    {
                        "part_kind": "tool-call",
                        "tool_name": name,
                        "args": {},
                        "tool_call_id": f"call_{name}_{len(messages)}",
                    },
                    {
                        "part_kind": "tool-return",
                        "tool_name": name,
                        "content": "ok",
                    },
                ]
            }
        )
    return json.dumps(messages).encode()


def test_analyze_pass_progress_empty_none():
    result = scv.analyze_pass_progress(None)
    assert result == {
        "total": 0,
        "edit_calls": 0,
        "progress_calls": 0,
        "stuck_same_tool": None,
        "last_non_progress_run": 0,
    }


def test_analyze_pass_progress_counts_edits():
    result = scv.analyze_pass_progress(
        _tool_msgs("read_file", "write_file", "edit_file", "delete_file")
    )
    assert result["total"] == 4
    assert result["edit_calls"] == 3  # write_file, edit_file, delete_file
    assert result["progress_calls"] == 0


def test_analyze_pass_progress_counts_progress_signals():
    result = scv.analyze_pass_progress(
        _tool_msgs("read_file", "run_command", "post_comment", "spawn_subtask")
    )
    assert result["total"] == 4
    assert result["edit_calls"] == 0
    assert result["progress_calls"] == 3  # run_command, post_comment, spawn_subtask


def test_analyze_pass_progress_no_stuck_same_tool_when_mixed():
    result = scv.analyze_pass_progress(
        _tool_msgs("read_ticket", "read_file", "write_file"),
        same_tool_window=3,
    )
    assert result["stuck_same_tool"] is None


def test_analyze_pass_progress_detects_stuck_same_tool():
    # Last 5 calls are all read_ticket (a non-progress tool).
    result = scv.analyze_pass_progress(
        _tool_msgs(
            "read_file",
            "list_dir",
            "read_ticket",
            "read_ticket",
            "read_ticket",
            "read_ticket",
            "read_ticket",
        ),
        same_tool_window=5,
    )
    assert result["stuck_same_tool"] == "read_ticket"
    assert result["last_non_progress_run"] == 7  # all 7 are non-progress


def test_analyze_pass_progress_stuck_window_not_met():
    # Only 4 consecutive read_ticket at tail — below window of 5.
    result = scv.analyze_pass_progress(
        _tool_msgs(
            "read_file", "read_ticket", "read_ticket", "read_ticket", "read_ticket"
        ),
        same_tool_window=5,
    )
    assert result["stuck_same_tool"] is None
    assert result["last_non_progress_run"] == 5


def test_analyze_pass_progress_stuck_reset_by_progress_tool():
    # run_command breaks the non-progress run, so the tail is only
    # read_ticket × 3 (below window of 5).
    result = scv.analyze_pass_progress(
        _tool_msgs(
            "read_ticket",
            "read_ticket",
            "run_command",
            "read_ticket",
            "read_ticket",
            "read_ticket",
        ),
        same_tool_window=5,
    )
    assert result["stuck_same_tool"] is None
    assert result["last_non_progress_run"] == 3


def test_analyze_pass_progress_edit_tool_breaks_stuck_run():
    result = scv.analyze_pass_progress(
        _tool_msgs(
            "read_ticket",
            "read_ticket",
            "read_ticket",
            "read_ticket",
            "read_ticket",
            "edit_file",
        ),
        same_tool_window=5,
    )
    assert result["stuck_same_tool"] is None
    assert result["last_non_progress_run"] == 0  # edit_file is not non-progress
    assert result["edit_calls"] == 1


def test_analyze_pass_progress_list_epic_children_loop():
    result = scv.analyze_pass_progress(
        _tool_msgs(
            "list_epic_children",
            "list_epic_children",
            "list_epic_children",
            "list_epic_children",
            "list_epic_children",
        ),
        same_tool_window=5,
    )
    assert result["stuck_same_tool"] == "list_epic_children"
    assert result["last_non_progress_run"] == 5


def test_analyze_pass_progress_malformed_json_fails_open():
    result = scv.analyze_pass_progress(b"{not json")
    assert result["total"] == 0
    assert result["stuck_same_tool"] is None
