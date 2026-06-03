from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

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


def _fake_run_writing_report(report_obj=None, returncode=0, stderr=""):
    """Build a fake ``subprocess.run`` that mimics the jscpd json reporter:

    it writes ``jscpd-report.json`` into the ``--output`` directory passed on
    the command line (when ``report_obj`` is not None), then returns a
    CompletedProcess with empty stdout (the real reporter writes to a file,
    not stdout).
    """

    def fake_run(cmd, *args, **kwargs):
        if report_obj is not None:
            out_idx = cmd.index("--output")
            out_dir = Path(cmd[out_idx + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "jscpd-report.json").write_text(json.dumps(report_obj))
        return subprocess.CompletedProcess(
            args=cmd, returncode=returncode, stdout="", stderr=stderr
        )

    return fake_run


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


def test_run_jscpd_missing_report(tmp_path, monkeypatch):
    """No report file written → descriptive error with exit code + stderr."""

    monkeypatch.setattr(
        jscpd_tool.subprocess,
        "run",
        _fake_run_writing_report(report_obj=None, returncode=1, stderr="some error"),
    )
    result = jscpd_tool.run_jscpd(tmp_path)
    assert result.startswith("ERROR:")
    assert "exit code 1" in result
    assert "some error" in result


def test_run_jscpd_empty_report(tmp_path, monkeypatch):
    """Empty report file → descriptive error with captured diagnostics."""

    def fake_run(cmd, *args, **kwargs):
        out_idx = cmd.index("--output")
        out_dir = Path(cmd[out_idx + 1])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "jscpd-report.json").write_text("")
        return subprocess.CompletedProcess(
            args=cmd, returncode=5, stdout="boom", stderr="bad output"
        )

    monkeypatch.setattr(jscpd_tool.subprocess, "run", fake_run)
    result = jscpd_tool.run_jscpd(tmp_path)
    assert result.startswith("ERROR:")
    assert "exit code 5" in result
    assert "bad output" in result


def test_run_jscpd_invalid_report(tmp_path, monkeypatch):
    """Non-JSON report file → descriptive error with captured diagnostics."""

    def fake_run(cmd, *args, **kwargs):
        out_idx = cmd.index("--output")
        out_dir = Path(cmd[out_idx + 1])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "jscpd-report.json").write_text("not valid json")
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout="", stderr="parse trouble"
        )

    monkeypatch.setattr(jscpd_tool.subprocess, "run", fake_run)
    result = jscpd_tool.run_jscpd(tmp_path)
    assert result.startswith("ERROR:")
    assert "exit code 1" in result
    assert "parse trouble" in result


def test_run_jscpd_nonzero_with_report_delegates_to_parse(tmp_path, monkeypatch):
    """Non-zero exit but valid report file → delegates to _parse_jscpd_output.

    jscpd exits non-zero when clones are found, so a non-zero return code is
    not by itself an error.
    """
    report = {
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

    monkeypatch.setattr(
        jscpd_tool.subprocess,
        "run",
        _fake_run_writing_report(report_obj=report, returncode=1),
    )
    result = jscpd_tool.run_jscpd(tmp_path)
    assert "1 clone pair(s) detected" in result
    assert "src/a.py:1-12" in result
    assert "src/b.py:34-45" in result


def test_run_jscpd_no_clones_from_report(tmp_path, monkeypatch):
    """Valid report with empty duplicates → 'no clone pairs detected'."""

    monkeypatch.setattr(
        jscpd_tool.subprocess,
        "run",
        _fake_run_writing_report(report_obj={"duplicates": []}, returncode=0),
    )
    result = jscpd_tool.run_jscpd(tmp_path)
    assert "no clone pairs detected" in result


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
        _fake_run_writing_report(report_obj={"duplicates": []}),
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


# ── smoke test (real jscpd) ────────────────────────────────────────────


def _jscpd_runnable() -> bool:
    """Probe whether ``npx jscpd@4`` can actually run in this environment."""
    try:
        proc = subprocess.run(
            ["npx", "jscpd@4", "--version"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


_CLONE_SNIPPET = """\
def compute_totals(records):
    total = 0
    count = 0
    for record in records:
        total = total + record.value
        count = count + 1
    average = total / count if count else 0
    return total, count, average
"""


def test_detect_duplication_smoke_real_clone(tmp_path):
    """End-to-end: a 2-file fixture with a deliberate clone reports >=1 pair.

    Guarded — skips cleanly when jscpd/npx is unavailable (e.g. no Node, or
    a ``--network none`` sandbox), so it never fails on such environments.
    """
    if not _jscpd_runnable():
        pytest.skip("jscpd/npx not runnable in this environment")

    # Minimal .jscpd.json so the wrapper's --config path resolves.
    (tmp_path / ".jscpd.json").write_text(
        json.dumps(
            {
                "mode": "strict",
                "minLines": 5,
                "minTokens": 40,
                "format": ["python"],
                "reporters": ["consoleFull"],
                "gitignore": False,
            }
        )
    )
    (tmp_path / "a.py").write_text(_CLONE_SNIPPET)
    (tmp_path / "b.py").write_text(_CLONE_SNIPPET)

    result = jscpd_tool.run_jscpd(tmp_path)

    assert "clone pair(s) detected" in result
    # Extract the reported count and assert it is >= 1.
    count = int(result.split("**")[1].split(" clone pair")[0])
    assert count >= 1
