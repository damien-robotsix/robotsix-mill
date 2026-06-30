"""Trimming helpers for retry/audit/re-refine passes and draft-artifact trimming.

When a stage re-invokes an agent on the same ticket (test-failure
retry, reviewer sendback, re-refine), the agent already knows the full
context from the first pass.  Re-sending the full accumulated lifecycle
context — spec, epic context, memory ledger, reference files — inflates
every call.  This module provides helpers to trim the context down to
the delta: the specific failing item plus a minimal spec reminder.

It also provides ``trim_large_artifacts`` for pre-refine draft trimming:
lockfile diffs and CI log dumps are the dominant input-token consumers
in the refine stage and can be summarised without losing the actionable
signal.
"""

from __future__ import annotations

import re

# Minimum draft length (characters) before trimming is considered.
_TRIM_MIN_CHARS: int = 4000

# Max lines of a lockfile diff block before it is summarised.
_TRIM_LOCKFILE_MAX_LINES: int = 50

# Max characters of a CI log block before it is summarised.
_TRIM_CI_LOG_MAX_CHARS: int = 3000

# Lockfile names whose diffs we recognise as trimmable.
_LOCKFILE_NAMES: frozenset[str] = frozenset(
    {
        "uv.lock",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Cargo.lock",
        "Gemfile.lock",
        "poetry.lock",
        "Pipfile.lock",
        "composer.lock",
        "mix.lock",
        "go.sum",
        "requirements.txt",
    }
)

# Signal keywords that indicate a CI log dump (case-insensitive search).
_CI_LOG_SIGNALS: list[str] = [
    "= FAILURES =",
    "FAILED",
    "short test summary",
    "=== FAIL",
    "--- FAIL",
    "[FAIL]",
    "tests failed",
    "test failed",
    "build failed",
    "exit code: 1",
    "Error: Process completed with exit code",
    "##[error]",
    "::error::",
]

_CI_LOG_SIGNAL_RE: re.Pattern[str] = re.compile(
    "|".join(re.escape(s) for s in _CI_LOG_SIGNALS), re.IGNORECASE
)


def trim_spec_for_retry(spec: str, *, max_chars: int = 800) -> str:
    """Return a minimal version of *spec* suitable for a retry pass.

    Keeps the first *max_chars* characters, advancing to the next
    paragraph boundary so the truncation is clean.  On a retry pass
    the agent already saw the full spec on the first pass; this
    reminder is just enough to re-orient it.
    """
    if len(spec) <= max_chars:
        return spec

    cut = spec.rfind("\n\n", 0, max_chars)
    if cut == -1:
        cut = spec.rfind("\n", 0, max_chars)
    if cut == -1:
        cut = max_chars

    omitted = len(spec) - cut
    return (
        spec[:cut] + f"\n\n[... spec truncated: {omitted} chars of detail omitted — "
        "you already read the full spec on the first pass]"
    )


def trim_draft_for_re_refine(draft: str, *, max_chars: int = 800) -> str:
    """Return a minimal version of *draft* for a refine re-refine pass.

    Keeps the first *max_chars* characters, advancing to the next
    paragraph boundary.  On a re-refine pass the agent only needs the
    reviewer's delta comments + a brief reminder of the draft's topic.
    """
    return trim_spec_for_retry(draft, max_chars=max_chars)


def _collect_lockfile_paths(draft: str) -> set[str]:
    """Scan draft for diff headers naming known lockfiles."""
    _lockfile_pat = re.compile(
        r"^(?:diff --git a/|--+ a/|\+\+\+ b/)(\S+)",
        re.MULTILINE,
    )
    paths: set[str] = set()
    for m in _lockfile_pat.finditer(draft):
        path = m.group(1)
        fn = path.rsplit("/", 1)[-1] if "/" in path else path
        if fn in _LOCKFILE_NAMES:
            paths.add(path)
    return paths


def _build_lockfile_block_pat(lf_path: str) -> re.Pattern[str]:
    """Compile a regex that matches the diff block for *lf_path*."""
    escaped = re.escape(lf_path)
    return re.compile(
        rf"^diff --git a/{escaped} b/{escaped}\n.*?"
        r"(?=\n\ndiff --git |\n\n(?![-+ @\\])|\Z)",
        re.MULTILINE | re.DOTALL,
    )


def _replace_lockfile_block(m: re.Match[str], max_lines: int) -> str:
    """Replace a single lockfile diff block with a summary if too large."""
    block = m.group(0)
    lines = block.split("\n")
    if len(lines) <= max_lines:
        return block
    header_lines: list[str] = []
    body_start = 0
    for i, line in enumerate(lines):
        header_lines.append(line)
        if line.startswith("@@") or line.startswith("---"):
            body_start = i + 1
            break
    kept = header_lines + lines[body_start : body_start + 5]
    omitted_count = max(0, len(lines) - len(kept))
    return "\n".join(kept) + (
        f"\n... [{omitted_count} lines of lockfile diff omitted — "
        f"the full diff is preserved in draft-original.md]"
    )


def _trim_ci_log_blocks(draft: str, max_chars: int) -> str:
    """Trim blocks in *draft* that look like CI log dumps."""
    blocks = re.split(r"\n\n+", draft)
    trimmed_blocks: list[str] = []
    for block in blocks:
        if len(block) > max_chars and _CI_LOG_SIGNAL_RE.search(block):
            kept = block[:500]
            omitted_chars = len(block) - 500
            trimmed_blocks.append(
                kept + f"\n\n[... {omitted_chars} chars of CI log output truncated — "
                "the full log is preserved in draft-original.md]"
            )
        else:
            trimmed_blocks.append(block)
    return "\n\n".join(trimmed_blocks)


def trim_large_artifacts(draft: str) -> str:
    """Trim lockfile diffs and CI log dumps from *draft*.

    Only fires when *draft* is large enough (>``_TRIM_MIN_CHARS``)
    AND a clear lockfile/CI-log signal is present.  When both
    conditions hold:

    - Any diff block whose header names a known lockfile and which
      exceeds ``_TRIM_LOCKFILE_MAX_LINES`` lines is replaced with a
      summary note + a line-count annotation.
    - Any sizable text block (paragraph or contiguous line block)
      containing CI-failure signal keywords and exceeding
      ``_TRIM_CI_LOG_MAX_CHARS`` characters is replaced with a
      summary note.

    Returns the (potentially trimmed) draft.  This is a deterministic,
    conservative heuristic — it only fires when the draft is large and
    the signal is unambiguous.
    """
    if len(draft) <= _TRIM_MIN_CHARS:
        return draft

    lockfile_paths = _collect_lockfile_paths(draft)
    _has_ci_signal = bool(_CI_LOG_SIGNAL_RE.search(draft))

    if not lockfile_paths and not _has_ci_signal:
        return draft

    trimmed = draft

    if lockfile_paths:
        for lf_path in sorted(lockfile_paths, key=len, reverse=True):
            pat = _build_lockfile_block_pat(lf_path)
            trimmed = pat.sub(
                lambda m: _replace_lockfile_block(m, _TRIM_LOCKFILE_MAX_LINES), trimmed
            )

    if _has_ci_signal:
        trimmed = _trim_ci_log_blocks(trimmed, _TRIM_CI_LOG_MAX_CHARS)

    return trimmed
