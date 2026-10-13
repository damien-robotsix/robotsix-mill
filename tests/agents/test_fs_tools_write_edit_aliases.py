"""``write_file`` / ``edit_file`` / ``verify_diff`` must accept Claude Code's
own argument spellings at the SCHEMA level.

On the Claude SDK tool path the JSON schema is validated BEFORE the tool body
runs, so every ``write_file(file_path=…)``, ``edit_file(…, replace_all=True)``
and ``verify_diff(description=…)`` call was rejected with ``Additional
properties are not allowed`` — five wasted turns in one mill window on
2026-10-13 (the same class that #3190 fixed for ``read_file``).
"""

from __future__ import annotations

from pydantic_ai import Tool

from robotsix_mill.agents.fs_tools import build_fs_tools
from robotsix_mill.agents.verify_diff_tool import make_verify_diff_tool


def _tool(root, settings, name):
    tools = build_fs_tools(root, settings)
    return next(t for t in tools if getattr(t, "__name__", "") == name)


def test_write_file_schema_accepts_claude_code_aliases(tmp_path, settings) -> None:
    schema = Tool(
        _tool(tmp_path, settings, "write_file")
    ).tool_def.parameters_json_schema
    assert {"path", "file_path", "target_file", "content"} <= set(schema["properties"])
    assert "path" not in (schema.get("required") or [])


def test_write_file_file_path_alias_writes(tmp_path, settings) -> None:
    write_file = _tool(tmp_path, settings, "write_file")
    out = write_file(file_path="notes.txt", content="hello\n")
    assert out.startswith("wrote")
    assert (tmp_path / "notes.txt").read_text() == "hello\n"


def test_write_file_without_path_or_content_errors(tmp_path, settings) -> None:
    write_file = _tool(tmp_path, settings, "write_file")
    assert write_file(content="x").startswith("error: write_file requires `path`")
    assert write_file(path="a.txt").startswith("error: write_file requires `content`")


def test_edit_file_schema_has_replace_all(tmp_path, settings) -> None:
    schema = Tool(
        _tool(tmp_path, settings, "edit_file")
    ).tool_def.parameters_json_schema
    assert "replace_all" in schema["properties"]


def test_edit_file_replace_all_replaces_every_occurrence(tmp_path, settings) -> None:
    (tmp_path / "a.py").write_text("x = 1\ny = x\nz = x\n")
    edit_file = _tool(tmp_path, settings, "edit_file")
    out = edit_file(path="a.py", old_string="x", new_string="w", replace_all=True)
    assert "replaced 3 occurrence(s)" in out
    assert (tmp_path / "a.py").read_text() == "w = 1\ny = w\nz = w\n"


def test_edit_file_without_replace_all_still_refuses_ambiguous(
    tmp_path, settings
) -> None:
    (tmp_path / "a.py").write_text("x = 1\ny = x\n")
    edit_file = _tool(tmp_path, settings, "edit_file")
    out = edit_file(path="a.py", old_string="x", new_string="w")
    assert "appears 2 times" in out


def test_verify_diff_schema_accepts_description(tmp_path) -> None:
    schema = Tool(make_verify_diff_tool(tmp_path)).tool_def.parameters_json_schema
    assert "description" in schema["properties"]
