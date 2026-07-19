from __future__ import annotations

import subprocess
from pathlib import Path

from robotsix_mill.agents import verify_diff_tool
from robotsix_mill.agents.tool_registry import ToolRegistry


def _completed_process(
    args=None,
    returncode=0,
    stdout="",
    stderr="",
):
    return subprocess.CompletedProcess(
        args=args or ["git", "-C", "/tmp", "diff", "--stat"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


# ── _parse_changed_files ───────────────────────────────────────────────


def test_parse_changed_files_single_file():
    result = verify_diff_tool._parse_changed_files(
        " src/robotsix_mill/agents/verify_diff_tool.py | 121 +++++++++++++"
    )
    assert result == {"src/robotsix_mill/agents/verify_diff_tool.py"}


def test_parse_changed_files_multiple_files():
    result = verify_diff_tool._parse_changed_files(
        " src/a.py | 5 ++++\n src/b.py | 3 ---"
    )
    assert result == {"src/a.py", "src/b.py"}


def test_parse_changed_files_with_summary_line():
    """Summary lines (no '|') are skipped."""
    result = verify_diff_tool._parse_changed_files(
        " src/a.py | 5 ++++\n 3 files changed, 8 insertions(+), 3 deletions(-)\n src/b.py | 3 ---"
    )
    assert result == {"src/a.py", "src/b.py"}


def test_parse_changed_files_empty_output():
    result = verify_diff_tool._parse_changed_files("")
    assert result == set()


def test_parse_changed_files_only_summary():
    """Only summary lines → empty set."""
    result = verify_diff_tool._parse_changed_files(
        " 4 files changed, 20 insertions(+), 5 deletions(-)"
    )
    assert result == set()


def test_parse_changed_files_line_with_pipe_no_path():
    """Edge case: line has '|' but nothing before it."""
    result = verify_diff_tool._parse_changed_files(" | 5 ++++")
    assert result == set()  # empty string stripped → skipped


def test_parse_changed_files_handles_leading_whitespace():
    """Paths with leading/trailing whitespace are stripped."""
    result = verify_diff_tool._parse_changed_files(
        "   path/with/spaces.py   | 10 +++++"
    )
    assert result == {"path/with/spaces.py"}


# ── _run_verify_diff ───────────────────────────────────────────────────


def test_run_verify_diff_success(tmp_path, monkeypatch):
    monkeypatch.setattr(
        verify_diff_tool.subprocess,
        "run",
        lambda *a, **k: _completed_process(
            stdout=" src/a.py | 3 +++\n src/b.py | 2 --"
        ),
    )
    result = verify_diff_tool._run_verify_diff(tmp_path)
    assert "git diff --stat:" in result
    assert "src/a.py" in result
    assert "src/b.py" in result


def test_run_verify_diff_git_not_available(tmp_path, monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(verify_diff_tool.subprocess, "run", fake_run)
    result = verify_diff_tool._run_verify_diff(tmp_path)
    assert result == "verify_diff: git not available"


def test_run_verify_diff_timeout(tmp_path, monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["git", "diff", "--stat"], timeout=15)

    monkeypatch.setattr(verify_diff_tool.subprocess, "run", fake_run)
    result = verify_diff_tool._run_verify_diff(tmp_path)
    assert result == "verify_diff: git diff --stat timed out"


def test_run_verify_diff_nonzero_rc(tmp_path, monkeypatch):
    monkeypatch.setattr(
        verify_diff_tool.subprocess,
        "run",
        lambda *a, **k: _completed_process(
            returncode=128, stderr="fatal: not a git repository"
        ),
    )
    result = verify_diff_tool._run_verify_diff(tmp_path)
    assert result.startswith("verify_diff: git diff --stat failed (rc=128)")
    assert "fatal: not a git repository" in result


def test_run_verify_diff_clean_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(
        verify_diff_tool.subprocess,
        "run",
        lambda *a, **k: _completed_process(stdout=""),
    )
    result = verify_diff_tool._run_verify_diff(tmp_path)
    assert result == "verify_diff: working tree is clean — no uncommitted changes"


# ── _run_verify_diff expected_files cross-check ────────────────────────


def test_run_verify_diff_all_expected_present(tmp_path, monkeypatch):
    monkeypatch.setattr(
        verify_diff_tool.subprocess,
        "run",
        lambda *a, **k: _completed_process(
            stdout=" src/a.py | 3 +++\n src/b.py | 2 --"
        ),
    )
    result = verify_diff_tool._run_verify_diff(
        tmp_path, expected_files=["src/a.py", "src/b.py"]
    )
    assert "verify_diff: all expected files present in diff" in result


def test_run_verify_diff_some_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        verify_diff_tool.subprocess,
        "run",
        lambda *a, **k: _completed_process(stdout=" src/a.py | 3 +++"),
    )
    result = verify_diff_tool._run_verify_diff(
        tmp_path, expected_files=["src/a.py", "src/b.py"]
    )
    assert "verify_diff WARNING: expected but NOT in diff" in result
    assert "src/b.py" in result


def test_run_verify_diff_some_unexpected(tmp_path, monkeypatch):
    monkeypatch.setattr(
        verify_diff_tool.subprocess,
        "run",
        lambda *a, **k: _completed_process(stdout=" src/a.py | 3 +++\n src/c.py | 1 +"),
    )
    result = verify_diff_tool._run_verify_diff(tmp_path, expected_files=["src/a.py"])
    assert "verify_diff NOTE: in diff but NOT expected" in result
    assert "src/c.py" in result


def test_run_verify_diff_both_missing_and_unexpected(tmp_path, monkeypatch):
    monkeypatch.setattr(
        verify_diff_tool.subprocess,
        "run",
        lambda *a, **k: _completed_process(stdout=" src/a.py | 3 +++\n src/c.py | 1 +"),
    )
    result = verify_diff_tool._run_verify_diff(
        tmp_path, expected_files=["src/a.py", "src/b.py"]
    )
    assert "verify_diff WARNING: expected but NOT in diff" in result
    assert "verify_diff NOTE: in diff but NOT expected" in result


# ── make_verify_diff_tool ──────────────────────────────────────────────


def test_make_verify_diff_tool_returns_callable(tmp_path, monkeypatch):
    monkeypatch.setattr(
        verify_diff_tool.subprocess,
        "run",
        lambda *a, **k: _completed_process(stdout=" src/a.py | 1 +"),
    )
    tool = verify_diff_tool.make_verify_diff_tool(tmp_path)
    assert callable(tool)
    result = tool()
    assert "src/a.py" in result


def test_make_verify_diff_tool_delegates_to_run_verify_diff(tmp_path, monkeypatch):
    """The inner closure passes through to _run_verify_diff with repo_dir."""
    calls: list[tuple[Path, list[str] | None]] = []

    def fake_run_verify_diff(repo_dir, expected_files=None):
        calls.append((repo_dir, expected_files))
        return "mock result"

    monkeypatch.setattr(verify_diff_tool, "_run_verify_diff", fake_run_verify_diff)
    tool = verify_diff_tool.make_verify_diff_tool(tmp_path)
    result = tool(expected_files=["a.py"])
    assert result == "mock result"
    assert len(calls) == 1
    assert calls[0] == (tmp_path, ["a.py"])


def test_make_verify_diff_tool_registers_in_registry(tmp_path, monkeypatch):
    ToolRegistry._tools.pop("verify_diff", None)

    monkeypatch.setattr(
        verify_diff_tool.subprocess,
        "run",
        lambda *a, **k: _completed_process(),
    )

    verify_diff_tool.make_verify_diff_tool(tmp_path)
    tools = ToolRegistry.list_tools()
    matches = [t for t in tools if t.name == "verify_diff"]
    assert len(matches) == 1
    info = matches[0]
    assert info.name == "verify_diff"
    assert info.category == "git"
    assert "expected_files" in info.parameters


def test_make_verify_diff_tool_no_double_registration(tmp_path, monkeypatch):
    ToolRegistry._tools.pop("verify_diff", None)

    monkeypatch.setattr(
        verify_diff_tool.subprocess,
        "run",
        lambda *a, **k: _completed_process(),
    )

    verify_diff_tool.make_verify_diff_tool(tmp_path)
    verify_diff_tool.make_verify_diff_tool(tmp_path)
    tools = ToolRegistry.list_tools()
    matches = [t for t in tools if t.name == "verify_diff"]
    assert len(matches) == 1
