"""Implementation-ready spec detection and validation for the refine stage.

When a spec's implementation section already contains complete,
ready-to-commit file changes (file paths + full fenced code blocks),
this module provides a cheap validation pass that bypasses the expensive
LLM refine agent — saving cost and latency.

Heuristic: a spec is "implementation-ready" when it pairs file paths
with fenced code blocks (`` ```yaml``, `` ```python``, etc.) that
contain the actual content to be written.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

from ...agents.refining import RefineResult

# Regex that picks up backtick-quoted paths like `src/foo/bar.py`
_PATH_RE = re.compile(r"`([^`]*/[^`]*\.[a-zA-Z]{1,10})`")

# Regex that detects file path annotations preceding code blocks:
#   - "# File: path/to/file"
#   - "File: path/to/file"
#   - bare path-like strings on their own line
_FILE_HINT_RE = re.compile(
    r"(?:#\s*File:\s*|File:\s*)"  # "# File: " or "File: "
    r"([^\s`]+)",  # the path
)

# Plain path-like strings (with a slash and a known extension)
_PLAIN_PATH_RE = re.compile(r"^([\w./-]+\.[a-zA-Z]{1,10})$")


def _is_implementation_ready(draft: str) -> bool:
    """Return True when *draft* looks like an implementation-ready spec.

    A spec is implementation-ready when it contains at least one fenced
    code block with a language hint paired with a file path found either
    in a preceding hint line or as a backtick-quoted path anywhere in the
    draft.
    """
    pairs = _extract_file_code_pairs(draft)
    return len(pairs) > 0


def _extract_file_code_pairs(draft: str) -> list[tuple[str, str, str]]:
    """Parse *draft* and return (file_path, language, code) triples.

    Scans for fenced code blocks with a language hint.  For each block,
    looks at up to 5 preceding lines for a file-path annotation
    (``# File: path``, ``File: path``, or a plain path-like line).
    Additionally, backtick-quoted paths found earlier in the draft that
    aren't yet assigned to a block are paired with the nearest subsequent
    block.

    Returns empty list when no valid file-path + code-block pairs are found.
    """
    if not draft:
        return []

    lines = draft.splitlines()

    # First pass: find all fenced code blocks with language hints
    code_blocks: list[tuple[int, str, int]] = []  # (start_line, lang, end_line)
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = re.match(r"^```(\w+)$", line)
        if m:
            lang = m.group(1)
            start = i
            # Find closing fence
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith("```"):
                j += 1
            end = j  # line index of closing fence
            code_blocks.append((start, lang, end))
            i = j + 1
        else:
            i += 1

    if not code_blocks:
        return []

    # Second pass: try to pair each block with a file path
    pairs: list[tuple[str, str, str]] = []

    for start, lang, end in code_blocks:
        file_path = _find_file_path_before_line(lines, start)
        if file_path:
            code = "\n".join(lines[start + 1 : end])
            pairs.append((file_path, lang, code))

    # If we found no pairs via preceding-line hints, try pairing
    # backtick-quoted paths from anywhere in the draft with the nearest
    # subsequent code block (simple heuristic).
    if not pairs:
        all_paths = _PATH_RE.findall(draft)
        if all_paths and code_blocks:
            # Use the first path with the first block as a fallback
            for block_idx, (start, lang, end) in enumerate(code_blocks):
                if block_idx < len(all_paths):
                    file_path = all_paths[block_idx]
                    code = "\n".join(lines[start + 1 : end])
                    pairs.append((file_path, lang, code))

    return pairs


def _find_file_path_before_line(lines: list[str], block_start: int) -> str | None:
    """Look at up to 5 lines before *block_start* for a file-path annotation.

    Checks in order:
    1. ``# File: <path>`` or ``File: <path>`` patterns
    2. Plain path-like strings (containing a slash + known extension)
    """
    lookback_start = max(0, block_start - 5)
    for i in range(block_start - 1, lookback_start - 1, -1):
        line = lines[i].strip()
        if not line:
            continue
        # Check for "# File: path" or "File: path"
        m = _FILE_HINT_RE.match(line)
        if m:
            return m.group(1)
        # Check for plain path-like strings
        m = _PLAIN_PATH_RE.match(line)
        if m:
            return m.group(1)
    return None


def _validate_implementation_ready_spec(
    draft: str, repo_dir: Path | None
) -> str | None:
    """Validate each proposed file change in an implementation-ready spec.

    Checks performed:
    - Target file exists (when *repo_dir* is provided)
    - YAML blocks are syntactically valid
    - Python blocks are syntactically valid
    - No forbidden patterns (e.g. ``${{ }}`` in workflow_call input defaults)

    Returns ``None`` when all checks pass, or an error message string
    describing the first failure.
    """
    pairs = _extract_file_code_pairs(draft)
    if not pairs:
        return "no file-path + code-block pairs found in draft"

    for file_path, lang, code in pairs:
        # Check file existence
        if repo_dir is not None:
            target = (repo_dir / file_path).resolve()
            # Security: ensure the resolved path is within repo_dir
            try:
                target.relative_to(repo_dir.resolve())
            except ValueError:
                return f"path escapes repo: {file_path}"
            if not target.exists():
                return f"target file does not exist: {file_path}"

        # Syntax validation
        if lang in ("yaml", "yml"):
            try:
                yaml.safe_load(code)
            except yaml.YAMLError as e:
                return f"invalid YAML in {file_path}: {e}"

        elif lang in ("py", "python"):
            try:
                ast.parse(code)
            except SyntaxError as e:
                return f"invalid Python in {file_path}: {e}"

        # Forbidden pattern: ${{ }} inside workflow_call input defaults
        # (this pattern causes GitHub Actions to fail at parse time)
        if "${{" in code and "workflow_call" in code:
            return (
                f"forbidden pattern in {file_path}: "
                "${{ }} expressions are not allowed in workflow_call "
                "input defaults — use a static default value instead"
            )

    return None


def _build_synthetic_refine_result(draft: str) -> RefineResult:
    """Build a synthetic ``RefineResult`` from a validated,
    implementation-ready draft.

    The draft is preserved as-is — no LLM generation needed.
    """
    return RefineResult(
        split=False,
        spec_markdown=draft,
        children=None,
        updated_memory="",
        title=None,
        epic_body=None,
        promote_to_epic=False,
        no_change_needed=False,
        no_change_rationale=None,
        file_map=None,
        reference_files=[],
        conversation_state=None,
    )
