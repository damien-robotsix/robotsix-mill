"""Regression tests for scripts/emit_config_schema.py.

Covers:
    * _compute_diff returns empty string when committed and generated match.
    * _compute_diff returns a non-empty unified diff when they differ.
    * Diff truncation at the configured max_lines.
"""

from __future__ import annotations

from pathlib import Path

from tests.script_loader import load_script

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "emit_config_schema.py"

_emit = load_script(_SCRIPT_PATH)

_compute_diff = _emit._compute_diff


def test_diff_empty_when_in_sync() -> None:
    """_compute_diff returns empty string when committed == generated."""
    same = '{"a": 1}\n'
    diff = _compute_diff(same, same)
    assert diff == ""


def test_diff_nonempty_when_drifted() -> None:
    """_compute_diff returns a non-empty unified diff when texts differ."""
    committed = '{"a": 1,\n "b": 2}\n'
    generated = '{"a": 1,\n "c": 3}\n'

    diff = _compute_diff(committed, generated)
    assert diff != ""
    # Unified diff should contain markers for added/removed lines.
    assert '"b": 2' in diff
    assert '"c": 3' in diff


def test_diff_includes_file_headers() -> None:
    """The unified diff uses the fromfile/tofile headers."""
    committed = '{"a": 1}\n'
    generated = '{"a": 2}\n'

    diff = _compute_diff(committed, generated, fromfile="before", tofile="after")
    assert "--- before" in diff
    assert "+++ after" in diff


def test_diff_truncation() -> None:
    """When diff exceeds max_lines, it is capped with a truncation note."""
    # Generate a diff with many lines (one per key difference).
    committed_lines = [f'  "k{i}": {i}' for i in range(100)]
    generated_lines = [f'  "k{i}": {i + 1}' for i in range(100)]
    committed = "{\n" + ",\n".join(committed_lines) + "\n}\n"
    generated = "{\n" + ",\n".join(generated_lines) + "\n}\n"

    max_lines = 20
    diff = _compute_diff(committed, generated, max_lines=max_lines)
    lines = diff.splitlines()
    # max_lines diff lines + 1 truncation-note line
    assert len(lines) <= max_lines + 1
    assert any("truncated" in line for line in lines)
