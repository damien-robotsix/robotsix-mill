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
