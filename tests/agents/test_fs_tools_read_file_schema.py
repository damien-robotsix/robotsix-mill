"""``read_file`` must accept ``limit=None`` at the SCHEMA level.

The docstring has always said "Pass ``None`` to read the entire file",
and the body handles ``None`` everywhere, but the annotation was a bare
``int``.  On the Claude SDK tool path the JSON schema is validated BEFORE
the tool body runs, so every ``limit: null`` call was rejected with
``Input validation error: None is not of type 'integer'`` (three such
rejections in one mill boot window on 2026-09-07) — a wasted turn each.
"""

from __future__ import annotations

from pydantic_ai import Tool

from robotsix_mill.agents.fs_tools import build_fs_tools


def _read_file_tool(root, settings):
    tools = build_fs_tools(root, settings)
    return next(t for t in tools if getattr(t, "__name__", "") == "read_file")


def test_read_file_schema_allows_null_limit(tmp_path, settings) -> None:
    schema = Tool(_read_file_tool(tmp_path, settings)).tool_def.parameters_json_schema
    limit = schema["properties"]["limit"]
    # pydantic renders ``int | None`` as anyOf[integer, null]
    types = {opt.get("type") for opt in limit.get("anyOf", [])} or {limit.get("type")}
    assert "null" in types, limit
    assert "integer" in types, limit


def test_read_file_limit_none_reads_whole_file(tmp_path, settings) -> None:
    target = tmp_path / "big.txt"
    target.write_text("\n".join(f"line {i}" for i in range(1, 401)) + "\n")
    read_file = _read_file_tool(tmp_path, settings)
    out = read_file(None, path="big.txt", limit=None)
    assert "line 1" in out
    assert "line 400" in out


# --- argument aliases (live 2026-09-08: 20 `file_path` + 1 `target_file`
# --- rejections and 4 `limit: "None"` rejections in one mill boot window) ---


def test_read_file_schema_accepts_claude_code_aliases(tmp_path, settings) -> None:
    schema = Tool(_read_file_tool(tmp_path, settings)).tool_def.parameters_json_schema
    assert {"path", "file_path", "target_file"} <= set(schema["properties"])
    # `path` is no longer required at the schema level: an alias may carry it.
    assert "path" not in (schema.get("required") or [])
    limit_types = {
        opt.get("type") for opt in schema["properties"]["limit"].get("anyOf", [])
    }
    assert {"integer", "string", "null"} <= limit_types, schema["properties"]["limit"]


def test_read_file_file_path_alias_reads(tmp_path, settings) -> None:
    (tmp_path / "a.txt").write_text("alpha\nbeta\n")
    (tmp_path / "b.txt").write_text("gamma\n")
    read_file = _read_file_tool(tmp_path, settings)
    assert "alpha" in read_file(None, file_path="a.txt")
    assert "gamma" in read_file(None, target_file="b.txt")
    assert read_file(None).startswith("error: read_file requires `path`")


def test_read_file_limit_string_none_reads_whole_file(tmp_path, settings) -> None:
    target = tmp_path / "big.txt"
    target.write_text("\n".join(f"line {i}" for i in range(1, 301)) + "\n")
    read_file = _read_file_tool(tmp_path, settings)
    out = read_file(None, path="big.txt", limit="None")
    assert "line 300" in out
    (tmp_path / "small.txt").write_text("l1\nl2\nl3\n")
    two = read_file(None, path="small.txt", limit="2")
    assert "l2" in two and "l3" not in two
    assert read_file(None, path="small.txt", limit="lots").startswith("error:")
