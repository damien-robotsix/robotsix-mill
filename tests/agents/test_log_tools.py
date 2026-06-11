"""Tests for the deployed-log query tool (``query_app_logs``)."""

from __future__ import annotations

import os
import time
from pathlib import Path

from robotsix_mill.agents.log_tools import make_log_query_tool
from robotsix_mill.agents.tool_registry import ToolRegistry


def _write(path: Path, content: str, age_hours: float = 0.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if age_hours:
        ts = time.time() - age_hours * 3600
        os.utime(path, (ts, ts))


def test_keyword_filter_case_insensitive(tmp_path):
    _write(
        tmp_path / "app.log",
        "INFO startup ok\nERROR imap connection refused\nWARN slow query\n",
    )
    tool = make_log_query_tool(tmp_path)
    out = tool(keywords="ERROR")
    assert "imap connection refused" in out
    assert "startup ok" not in out
    assert "slow query" not in out

    # space-separated terms are OR'd, case-insensitive
    out2 = tool(keywords="error warn")
    assert "imap connection refused" in out2
    assert "slow query" in out2
    assert "startup ok" not in out2


def test_since_hours_excludes_stale_files(tmp_path):
    _write(tmp_path / "fresh.log", "recent boom\n", age_hours=1)
    _write(tmp_path / "stale.log", "old boom\n", age_hours=48)
    tool = make_log_query_tool(tmp_path)
    out = tool(keywords="boom", since_hours=24)
    assert "recent boom" in out
    assert "old boom" not in out


def test_max_lines_truncation_marked(tmp_path):
    lines = "".join(f"match line {i}\n" for i in range(50))
    _write(tmp_path / "app.log", lines)
    tool = make_log_query_tool(tmp_path)
    out = tool(keywords="match", max_lines=10)
    returned = [ln for ln in out.splitlines() if ln.startswith("app.log:")]
    assert len(returned) == 10
    assert "... (truncated, 40 more matching lines)" in out


def test_empty_keywords_returns_recent_lines(tmp_path):
    _write(tmp_path / "app.log", "alpha\nbeta\ngamma\n")
    tool = make_log_query_tool(tmp_path)
    out = tool(max_lines=200)
    assert "alpha" in out
    assert "gamma" in out


def test_missing_dir_returns_graceful_string(tmp_path):
    tool = make_log_query_tool(tmp_path / "does-not-exist")
    out = tool(keywords="boom")
    assert isinstance(out, str)
    assert "missing or not a directory" in out


def test_empty_dir_returns_graceful_string(tmp_path):
    tool = make_log_query_tool(tmp_path)
    out = tool(keywords="boom")
    assert isinstance(out, str)
    assert "No log files" in out


def test_binary_files_skipped(tmp_path):
    _write(tmp_path / "app.log", "ERROR text boom\n")
    _write(tmp_path / "archive.gz", "ERROR binary boom\n")
    tool = make_log_query_tool(tmp_path)
    out = tool(keywords="boom")
    assert "app.log" in out
    assert "archive.gz" not in out


def test_tool_registered(tmp_path):
    make_log_query_tool(tmp_path)
    names = {t.name for t in ToolRegistry.list_tools()}
    assert "query_app_logs" in names
