from __future__ import annotations

import json
import subprocess

from robotsix_mill.agents import jscpd_tool
from robotsix_mill.agents.tool_registry import ToolRegistry


# ── helpers ────────────────────────────────────────────────────────────


def _completed_process(
    args=None,
    returncode=0,
    stdout="",
    stderr="",
):
    return subprocess.CompletedProcess(
        args=args or ["npx", "jscpd@4", "..."],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


# ── run_jscpd ──────────────────────────────────────────────────────────


def test_run_jscpd_not_available(tmp_path, monkeypatch):
    """FileNotFoundError → 'ERROR: jscpd is not available'."""

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("npx")

    monkeypatch.setattr(jscpd_tool.subprocess, "run", fake_run)
    result = jscpd_tool.run_jscpd(tmp_path)
    assert result.startswith("ERROR: jscpd is not available")


def test_run_jscpd_timed_out(tmp_path, monkeypatch):
    """TimeoutExpired → 'ERROR: jscpd timed out'."""

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["npx", "jscpd@4"], timeout=120)

    monkeypatch.setattr(jscpd_tool.subprocess, "run", fake_run)
    result = jscpd_tool.run_jscpd(tmp_path)
    assert result.startswith("ERROR: jscpd timed out")


def test_run_jscpd_os_error(tmp_path, monkeypatch):
    """OSError → error string containing both 'could not run jscpd'
    and the OS error message."""

    def fake_run(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(jscpd_tool.subprocess, "run", fake_run)
    result = jscpd_tool.run_jscpd(tmp_path)
    assert "could not run jscpd" in result
    assert "disk full" in result


def test_run_jscpd_nonzero_empty_stdout(tmp_path, monkeypatch):
    """Non-zero exit + empty stdout → formatted error with exit code
    and stderr text."""

    def fake_run(*args, **kwargs):
        return _completed_process(returncode=1, stdout="", stderr="some error")

    monkeypatch.setattr(jscpd_tool.subprocess, "run", fake_run)
    result = jscpd_tool.run_jscpd(tmp_path)
    assert result.startswith("ERROR: jscpd exited with code 1")
    assert "some error" in result


def test_run_jscpd_nonzero_with_stdout_delegates_to_parse(tmp_path, monkeypatch):
    """Non-zero exit + non-empty stdout → delegates to _parse_jscpd_output."""
    clone_json = json.dumps(
        {
            "duplicates": [
                {
                    "format": "javascript",
                    "lines": 12,
                    "tokens": 45,
                    "firstFile": {"name": "src/a.py", "start": 1, "end": 12},
                    "secondFile": {"name": "src/b.py", "start": 34, "end": 45},
                }
            ]
        }
    )

    def fake_run(*args, **kwargs):
        return _completed_process(returncode=1, stdout=clone_json, stderr="")

    monkeypatch.setattr(jscpd_tool.subprocess, "run", fake_run)
    result = jscpd_tool.run_jscpd(tmp_path)
    assert "1 clone pair(s) detected" in result
    assert "src/a.py:1-12" in result
    assert "src/b.py:34-45" in result


# ── _parse_jscpd_output ────────────────────────────────────────────────


def test_parse_jscpd_output_with_clones():
    """Valid JSON with clone pairs produces structured human-readable output."""
    stdout = json.dumps(
        {
            "duplicates": [
                {
                    "format": "javascript",
                    "lines": 12,
                    "tokens": 45,
                    "firstFile": {"name": "src/a.py", "start": 1, "end": 12},
                    "secondFile": {"name": "src/b.py", "start": 34, "end": 45},
                }
            ]
        }
    )
    result = jscpd_tool._parse_jscpd_output(stdout)
    assert "1 clone pair(s) detected" in result
    assert "src/a.py:1-12" in result
    assert "src/b.py:34-45" in result
    assert "↔" in result
    assert "12 lines, 45 tokens" in result


def test_parse_jscpd_output_no_clones():
    """Empty duplicates array → 'no clone pairs detected'."""
    stdout = json.dumps({"duplicates": []})
    result = jscpd_tool._parse_jscpd_output(stdout)
    assert "no clone pairs detected" in result


def test_parse_jscpd_output_invalid_json():
    """Invalid JSON → error string."""
    result = jscpd_tool._parse_jscpd_output("not valid json")
    assert result.startswith("ERROR: could not parse jscpd JSON output")


# ── make_jscpd_tool ────────────────────────────────────────────────────


def test_make_jscpd_tool_returns_callable(tmp_path, monkeypatch):
    """The factory returns a callable that delegates to run_jscpd."""
    monkeypatch.setattr(
        jscpd_tool.subprocess,
        "run",
        lambda *a, **k: _completed_process(
            stdout=json.dumps({"duplicates": []}),
        ),
    )
    tool = jscpd_tool.make_jscpd_tool(tmp_path)
    assert callable(tool)
    result = tool()
    assert "no clone pairs detected" in result


def test_make_jscpd_tool_registers_in_registry(tmp_path, monkeypatch):
    """make_jscpd_tool registers 'detect_duplication' in ToolRegistry."""
    ToolRegistry._tools.pop("detect_duplication", None)

    monkeypatch.setattr(
        jscpd_tool.subprocess,
        "run",
        lambda *a, **k: _completed_process(),
    )

    jscpd_tool.make_jscpd_tool(tmp_path)
    tools = ToolRegistry.list_tools()
    matches = [t for t in tools if t.name == "detect_duplication"]
    assert len(matches) == 1
    info = matches[0]
    assert info.name == "detect_duplication"
    assert info.category == "exploration"
    assert info.parameters == {}


def test_make_jscpd_tool_no_double_registration(tmp_path, monkeypatch):
    """Calling make_jscpd_tool twice still results in exactly one entry."""
    ToolRegistry._tools.pop("detect_duplication", None)

    monkeypatch.setattr(
        jscpd_tool.subprocess,
        "run",
        lambda *a, **k: _completed_process(),
    )

    jscpd_tool.make_jscpd_tool(tmp_path)
    jscpd_tool.make_jscpd_tool(tmp_path)
    tools = ToolRegistry.list_tools()
    matches = [t for t in tools if t.name == "detect_duplication"]
    assert len(matches) == 1
