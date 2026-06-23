"""``detect_duplication`` agent tool — deterministic copy-paste detection
via jscpd (AST/token-based clone detection).

Provides a reproducible, non-LLM detector to replace visual inspection
for the audit agent.  The tool scans the entire repo with jscpd and
returns structured clone-pair data suitable for LLM consumption.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from collections.abc import Callable
from typing import Any

from ..runtime.tracing import trace_stage


def run_jscpd(repo_dir: Path) -> str:
    """Run jscpd and return a structured summary of clone pairs.

    Returns a human-readable string with, for each clone pair: both
    file paths, line ranges (``start``–``end``), duplicated line
    count, and token count.  If jscpd is unavailable or fails, returns
    a descriptive error string instead of raising.
    """
    # Use a wrapper-controlled output directory so the JSON report location
    # is deterministic and independent of ``.jscpd.json``'s ``output`` value.
    # A per-invocation temp dir avoids collisions and stale reports across
    # concurrent/repeat runs.
    output_dir = tempfile.mkdtemp(prefix="jscpd-")
    try:
        try:
            result = subprocess.run(
                [
                    "npx",
                    "jscpd@4",
                    "--config",
                    ".jscpd.json",
                    "--reporters",
                    "json",
                    "--output",
                    output_dir,
                    "--mode",
                    "strict",
                    ".",
                ],
                capture_output=True,
                text=True,
                cwd=str(repo_dir),
                timeout=120,
            )
        except FileNotFoundError:
            return (
                "ERROR: jscpd is not available — ``npx`` (Node.js) could not be "
                "found. Install Node.js and npm to use deterministic copy-paste "
                "detection."
            )
        except subprocess.TimeoutExpired:
            return (
                "ERROR: jscpd timed out after 120 seconds. The repository may be "
                "too large for a single-pass scan."
            )
        except OSError as exc:
            return f"ERROR: could not run jscpd — {exc}"

        # The json reporter writes ``jscpd-report.json`` inside ``--output``.
        # jscpd exits non-zero when clones are found (linter-like), so success
        # is decided by whether a valid report file can be read, not on the
        # return code.
        report_path = Path(output_dir) / "jscpd-report.json"
        try:
            report_text = report_path.read_text()
        except OSError:
            report_text = ""

        if not report_text.strip():
            return _jscpd_diagnostics_error(result)

        try:
            json.loads(report_text)
        except json.JSONDecodeError:
            return _jscpd_diagnostics_error(result)

        return _parse_jscpd_output(report_text)
    finally:
        shutil.rmtree(output_dir, ignore_errors=True)


def _jscpd_diagnostics_error(result: subprocess.CompletedProcess) -> str:
    """Build a descriptive ``ERROR:`` string surfacing raw jscpd diagnostics.

    Used when no valid ``jscpd-report.json`` could be read, so that
    failures are diagnosable (exit code + truncated stdout/stderr) instead
    of producing a bare parse error.
    """
    return (
        f"ERROR: jscpd did not produce a readable JSON report "
        f"(exit code {result.returncode}). "
        f"stdout: {result.stdout.strip()[:500]} "
        f"stderr: {result.stderr.strip()[:500]}"
    )


def _parse_jscpd_output(stdout: str) -> str:
    """Parse jscpd JSON output into a human-readable summary string.

    Each clone pair is rendered as:

        `file_a:lines N-M` ↔ `file_b:lines N-M` — X lines, Y tokens

    followed by a blank line for readability.  When no clones are
    found, returns a short "no clones detected" message.
    """
    try:
        data: dict[str, Any] = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return f"ERROR: could not parse jscpd JSON output — {exc}"

    duplications: list[dict[str, Any]] = data.get("duplicates", [])

    if not duplications:
        return (
            "jscpd scan complete — **no clone pairs detected** "
            "above the configured thresholds (minLines=5, minTokens=40)."
        )

    lines: list[str] = [
        f"jscpd scan complete — **{len(duplications)} clone pair(s) detected**",
        "",
    ]

    for i, clone in enumerate(duplications):
        fmt = clone.get("format", "unknown")
        lines.append(f"### Clone pair {i + 1} ({fmt})")

        dup_lines = clone.get("lines", "?")
        dup_tokens = clone.get("tokens", "?")

        first_file = clone.get("firstFile", {})
        second_file = clone.get("secondFile", {})

        fa_name = first_file.get("name", "?")
        fa_start = first_file.get("start", "?")
        fa_end = first_file.get("end", "?")
        fb_name = second_file.get("name", "?")
        fb_start = second_file.get("start", "?")
        fb_end = second_file.get("end", "?")

        lines.append(
            f"- `{fa_name}:{fa_start}-{fa_end}` ↔ `{fb_name}:{fb_start}-{fb_end}`"
        )
        lines.append(f"  — {dup_lines} lines, {dup_tokens} tokens")
        lines.append("")

    lines.append(
        "ℹ To review a specific clone pair, use ``read_file`` on the file paths above."
    )
    return "\n".join(lines)


def make_jscpd_tool(repo_dir: Path) -> Callable[[], str]:
    """Create the ``detect_duplication`` tool closure.

    Follows the same factory pattern as ``make_explore_tool``:
    wraps ``run_jscpd`` in a closure and self-registers into
    ``ToolRegistry``.
    """

    def detect_duplication() -> str:
        """Run jscpd to detect copy-paste duplication across the repository,
        returning clone pairs with file paths, line ranges, and duplication
        metrics."""
        with trace_stage("detect_duplication"):
            return run_jscpd(repo_dir)

    from .tool_registry import ToolInfo, ToolRegistry

    if not any(t.name == "detect_duplication" for t in ToolRegistry.list_tools()):
        ToolRegistry.register(
            ToolInfo(
                name="detect_duplication",
                description=(
                    "Run jscpd to detect copy-paste duplication across the "
                    "repository, returning clone pairs with file paths, line "
                    "ranges, and duplication metrics."
                ),
                category="exploration",
                parameters={},
            )
        )

    return detect_duplication
