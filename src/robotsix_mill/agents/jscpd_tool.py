"""``detect_duplication`` agent tool — deterministic copy-paste detection
via jscpd (AST/token-based clone detection).

Provides a reproducible, non-LLM detector to replace visual inspection
for the audit agent.  The tool scans the entire repo with jscpd and
returns structured clone-pair data suitable for LLM consumption.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from collections.abc import Callable
from typing import Any


def run_jscpd(repo_dir: Path) -> str:
    """Run jscpd and return a structured summary of clone pairs.

    Returns a human-readable string with, for each clone pair: both
    file paths, line ranges (``start``–``end``), duplicated line
    count, and token count.  If jscpd is unavailable or fails, returns
    a descriptive error string instead of raising.
    """
    try:
        result = subprocess.run(
            [
                "npx",
                "jscpd@4",
                "--config",
                ".jscpd.json",
                "--reporters",
                "json",
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

    # jscpd exits non-zero when clones are found (behaves like a linter).
    # Treat empty stdout + non-zero as a genuine error; non-empty stdout
    # (even with non-zero exit) is parseable JSON with findings.
    if result.returncode != 0 and not result.stdout.strip():
        return (
            f"ERROR: jscpd exited with code {result.returncode}. "
            f"stderr: {result.stderr.strip()[:500]}"
        )

    return _parse_jscpd_output(result.stdout)


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
        lines.append(f"### Clone pair {i+1} ({fmt})")

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
            f"- `{fa_name}:{fa_start}-{fa_end}` ↔ "
            f"`{fb_name}:{fb_start}-{fb_end}`"
        )
        lines.append(f"  — {dup_lines} lines, {dup_tokens} tokens")
        lines.append("")

    lines.append(
        "ℹ To review a specific clone pair, use ``read_file`` on the "
        "file paths above."
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
        return run_jscpd(repo_dir)

    from .tool_registry import ToolInfo, ToolRegistry

    if not any(t.name == "detect_duplication" for t in ToolRegistry.list_tools()):
        ToolRegistry.register(ToolInfo(
            name="detect_duplication",
            description=(
                "Run jscpd to detect copy-paste duplication across the "
                "repository, returning clone pairs with file paths, line "
                "ranges, and duplication metrics."
            ),
            category="exploration",
            parameters={},
        ))

    return detect_duplication
