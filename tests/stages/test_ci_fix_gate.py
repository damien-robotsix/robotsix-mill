"""Tests for the self-introduced CI gate detection (ci_fix_gate)."""

import subprocess

from robotsix_mill.stages.ci_fix_gate import (
    command_added_by_diff,
    failing_run_commands,
    self_introduced_gate_commands,
    workflow_added_lines,
    workflow_toolchain_pins,
)

_DART_CMD = "dart format --output=none --set-exit-if-changed ."

_LOG = (
    "2026-10-04T19:00:00.0000000Z ##[group]Run actions/checkout@abc\n"
    "2026-10-04T19:00:01.0000000Z ##[endgroup]\n"
    f"2026-10-04T19:00:02.0000000Z ##[group]Run {_DART_CMD}\n"
    f"2026-10-04T19:00:02.1000000Z {_DART_CMD}\n"
    "2026-10-04T19:00:02.2000000Z ##[endgroup]\n"
    "2026-10-04T19:00:05.0000000Z Changed lib/main.dart\n"
    "2026-10-04T19:00:05.1000000Z ##[error]Process completed with exit code 1.\n"
)

_BASE_WF = """name: CI
on: [push]
jobs:
  analyze:
    runs-on: ubuntu-latest
    steps:
      - uses: subosito/flutter-action@abc
        with:
          flutter-version: 3.47.2
      - run: flutter analyze
"""


def _git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _repo_with_branch(tmp_path, branch_wf: str):
    """A clone whose origin/main has _BASE_WF and HEAD rewrites it."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    (origin / ".github" / "workflows").mkdir(parents=True)
    (origin / ".github" / "workflows" / "ci.yml").write_text(_BASE_WF)
    _git(origin, "add", ".")
    _git(origin, "commit", "-q", "-m", "base")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(clone)],
        check=True,
        capture_output=True,
    )
    (clone / ".github" / "workflows" / "ci.yml").write_text(branch_wf)
    _git(clone, "commit", "-q", "-am", "add gate")
    return clone


def test_failing_run_commands_picks_step_before_error():
    assert failing_run_commands(_LOG) == [_DART_CMD]
    # No error → no failing step.
    assert failing_run_commands(_LOG.split("Changed")[0]) == []


def test_command_added_by_diff_matches_run_lines():
    assert command_added_by_diff(_DART_CMD, [f"      - run: {_DART_CMD}"])
    assert command_added_by_diff(_DART_CMD, [f"          {_DART_CMD}"])
    assert not command_added_by_diff(_DART_CMD, ["      - run: flutter analyze"])
    assert not command_added_by_diff("", [""])


def test_diff_added_gate_step_detected(tmp_path):
    clone = _repo_with_branch(tmp_path, _BASE_WF + f"      - run: {_DART_CMD}\n")
    added = workflow_added_lines(clone, "main")
    assert any(_DART_CMD in ln for ln in added)
    assert self_introduced_gate_commands(_LOG, clone, "main") == [_DART_CMD]


def test_pre_existing_gate_step_not_detected(tmp_path):
    log = _LOG.replace(_DART_CMD, "flutter analyze")
    # Branch only touches an unrelated line; the failing step pre-exists.
    clone = _repo_with_branch(tmp_path, _BASE_WF.replace("name: CI", "name: CI2"))
    assert self_introduced_gate_commands(log, clone, "main") == []


def test_git_error_degrades_to_not_introduced(tmp_path):
    assert workflow_added_lines(tmp_path, "main") == []
    assert self_introduced_gate_commands(_LOG, tmp_path, "main") == []


def test_workflow_toolchain_pins(tmp_path):
    clone = _repo_with_branch(tmp_path, _BASE_WF + f"      - run: {_DART_CMD}\n")
    pins = workflow_toolchain_pins(clone, [_DART_CMD])
    assert pins == [".github/workflows/ci.yml: flutter-version: 3.47.2"]
