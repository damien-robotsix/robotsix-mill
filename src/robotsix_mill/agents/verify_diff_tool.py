"""A ``verify_diff`` tool that returns a single diff summary, replacing
3-5 ``run_command`` (grep/awk) calls the implement agent would
otherwise issue after every ``edit_file``.

The tool runs ``git diff --stat`` once and optionally cross-checks
that a caller-provided list of expected file paths appear in the diff.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)


def _run_verify_diff(
    repo_dir: Path,
    expected_files: list[str] | None = None,
) -> str:
    """Run ``git diff --stat`` and optionally cross-check expected files."""
    try:
        result = subprocess.run(  # noqa: S603 — repo_dir is a controlled Path, not user input
            ["git", "-C", str(repo_dir), "diff", "--stat"],  # noqa: S607 — git is on PATH
            capture_output=True,
            text=True,
            timeout=15,
        )
    except FileNotFoundError:
        return "verify_diff: git not available"
    except subprocess.TimeoutExpired:
        return "verify_diff: git diff --stat timed out"

    if result.returncode != 0:
        stderr = result.stderr.strip()
        return f"verify_diff: git diff --stat failed (rc={result.returncode}): {stderr}"

    stat_output = result.stdout.strip()
    if not stat_output:
        return "verify_diff: working tree is clean — no uncommitted changes"

    lines = [f"git diff --stat:\n{stat_output}"]

    # Optional: cross-check expected file fingerprints.
    if expected_files:
        changed = _parse_changed_files(stat_output)
        missing = [f for f in expected_files if f not in changed]
        unexpected = [f for f in changed if f not in expected_files]
        if missing:
            lines.append(f"verify_diff WARNING: expected but NOT in diff: {missing}")
        if unexpected:
            lines.append(f"verify_diff NOTE: in diff but NOT expected: {unexpected}")
        if not missing and not unexpected:
            lines.append("verify_diff: all expected files present in diff")

    return "\n".join(lines)


def _parse_changed_files(stat_output: str) -> set[str]:
    """Parse ``git diff --stat`` output and return the set of changed file paths."""
    changed: set[str] = set()
    for line in stat_output.strip().splitlines():
        # Each stat line looks like: " path/to/file.py | 3 ++-"
        # Skip summary lines (e.g. " 3 files changed, ...")
        if "|" not in line:
            continue
        file_path = line.split("|")[0].strip()
        if file_path:
            changed.add(file_path)
    return changed


def make_verify_diff_tool(repo_dir: Path) -> Callable[..., str]:
    """Return the ``verify_diff`` closure bound to *repo_dir*.

    The returned function is synchronous (subprocess call) so it works
    with both sync and async pydantic-ai tool dispatch.
    """

    def verify_diff(expected_files: list[str] | None = None) -> str:
        """Run ``git diff --stat`` and return a single diff summary.

        Call this ONCE after a batch of edits instead of verifying
        each file serially with ``run_command`` grep/awk calls.  The
        ``--stat`` output lists every changed file with line counts.

        Args:
            expected_files: Optional list of relative file paths you
                expect to appear in the diff.  When provided the tool
                cross-checks them and reports any missing or unexpected
                files.

        Returns:
            A string containing the ``git diff --stat`` output plus
            (when ``expected_files`` is provided) a cross-check note.
        """
        return _run_verify_diff(repo_dir, expected_files)

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="verify_diff",
            description=(
                "Run ``git diff --stat`` once and return a single diff "
                "summary. Optionally cross-check that expected file paths "
                "appear in the diff. Replaces multiple ``run_command`` "
                "grep/awk verification calls after each ``edit_file`` "
                "with one consolidated check."
            ),
            category="git",
            parameters={
                "expected_files": "list[str] | None (optional list of "
                "file paths expected in the diff)"
            },
        )
    )

    return verify_diff
