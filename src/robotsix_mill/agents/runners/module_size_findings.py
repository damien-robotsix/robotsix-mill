"""Per-finding measurement and epic report for the module_size pass.

The module_size scan files one child ticket per finding (it is not
collapsed into a single rollup ticket). Each child gets a deterministic
measurement block (path, line count, top-level symbol count, counting
method, commit SHA) computed from the cloned tree, so a downstream
ticket never has to re-run the scan to recover its own numbers. The
full untruncated report is posted on the parent rollup epic.
"""

from __future__ import annotations

import ast
import logging
import re
import subprocess
from pathlib import Path

log = logging.getLogger("robotsix_mill.module_size_findings")

COUNTING_METHOD = (
    "lines = number of newline characters in the file; top-level symbols = "
    "module-level `class`, `def` and `async def` statements (Python `ast`)"
)

# ``module size: split <path> (N lines, M top-level symbols)`` — the
# draft title format the module_size prompt prescribes. Falls back to the
# first ``*.py`` path-looking token in the body.
_TITLE_PATH_RE = re.compile(r"split\s+`?([^\s`()]+\.py)`?")
_ANY_PATH_RE = re.compile(r"`?((?:[\w.-]+/)+[\w.-]+\.py)`?")


def extract_path(title: str, body: str) -> str:
    """Return the repo-relative file path a finding is about, or ``""``."""
    m = _TITLE_PATH_RE.search(title) or _ANY_PATH_RE.search(body)
    return m.group(1) if m else ""


def head_sha(repo_dir: Path | None) -> str:
    """Return the commit SHA checked out in *repo_dir*, or ``""``."""
    if repo_dir is None:
        return ""
    try:
        out = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except OSError, subprocess.SubprocessError:
        return ""
    return out.stdout.strip()


def measure(repo_dir: Path, rel_path: str) -> tuple[int, int | None] | None:
    """Return ``(line_count, top_level_symbol_count)`` for *rel_path*.

    The path is confined to *repo_dir*; ``None`` when it escapes the
    tree or is not a readable file. The symbol count is ``None`` when the
    file does not parse.
    """
    root = repo_dir.resolve()
    target = (root / rel_path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return None
    try:
        data = target.read_bytes()
    except OSError:
        return None
    lines = data.count(b"\n")
    try:
        tree = ast.parse(data)
    except SyntaxError, ValueError:
        return lines, None
    symbols = sum(
        isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        for node in tree.body
    )
    return lines, symbols


def measurement_block(
    repo_dir: Path | None, title: str, body: str, commit_sha: str
) -> str:
    """Render the deterministic measurement section for one finding."""
    path = extract_path(title, body)
    measured = measure(repo_dir, path) if repo_dir is not None and path else None
    if measured is None:
        lines_s = symbols_s = "not measured (file not found in the scanned tree)"
    else:
        lines_s = str(measured[0])
        symbols_s = "unparseable" if measured[1] is None else str(measured[1])
    return (
        "## Measurement\n\n"
        f"- **Path:** `{path or 'unknown'}`\n"
        f"- **Line count:** {lines_s}\n"
        f"- **Top-level symbol count:** {symbols_s}\n"
        f"- **Counting method:** {COUNTING_METHOD}\n"
        f"- **Commit:** `{commit_sha or 'unknown'}`"
    )


def render_report(
    titles: list[str], bodies: list[str], created: list[dict[str, str]]
) -> str:
    """Render the full, untruncated scan report for the parent epic."""
    by_title = {c["title"]: c["id"] for c in created}
    sections = [f"# module_size scan report: {len(titles)} finding(s)"]
    for i, (title, body) in enumerate(zip(titles, bodies, strict=True), 1):
        child = by_title.get(title)
        ref = (
            f"child ticket `{child}`"
            if child
            else ("no child filed (deduped, skipped or over the per-run draft cap)")
        )
        sections.append(f"## {i}. {title}\n\n_{ref}_\n\n{body}")
    return "\n\n".join(sections)
