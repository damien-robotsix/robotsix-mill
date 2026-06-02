"""Pre-refine freshness gate.

Verifies that file paths and line ranges cited in a ticket draft
still exist on the current HEAD of the working branch.  When the
cited evidence has gone stale — upstream rewrite, sibling commit,
or hallucinated finding — the ticket is short-circuited to DONE
before the expensive refine agent runs.

The gate is deterministic (no LLM call): it extracts file paths
from the draft text with regex and checks their existence on disk.
A ticket is flagged stale only when it cites multiple file paths
and the majority cannot be verified — a single missing path
(possible typo) does not trigger the gate.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger("robotsix_mill.agents.freshness")

# Known source-file extensions.  Keep this list small and conservative
# — we only want to match paths that look like real source files, not
# prose fragments that happen to contain a dot.
_SOURCE_EXTENSIONS = (
    "py",
    "md",
    "yaml",
    "yml",
    "json",
    "toml",
    "cfg",
    "ini",
    "txt",
    "js",
    "ts",
    "jsx",
    "tsx",
    "css",
    "html",
    "rst",
    "sh",
    "sql",
    "graphql",
    "proto",
)

# Match backtick-quoted strings: `...`
_BACKTICK_RE = re.compile(r"`([^`]+)`")

# Match bare paths that look like file references (word/…/word.ext).
# The pattern requires at least one directory separator so we don't
# match every single word that happens to end in ".py".  Also captures
# optional trailing line-range (:NN or :NN-NN).
_EXT_ALT = "|".join(_SOURCE_EXTENSIONS)
_BARE_PATH_RE = re.compile(
    rf"(?<!\w)([a-zA-Z0-9_\-./]+\.(?:{_EXT_ALT})(?::\d+(?:-\d+)?)?)(?!\w)"
)

# Match a trailing line-range suffix: :NN or :NN-NN
_LINE_RANGE_RE = re.compile(r":(\d+)(?:-(\d+))?$")


def _resolve_path(raw: str, repo_dir: Path) -> tuple[str, bool, int | None, int | None]:
    """Resolve a single cited path against *repo_dir*.

    Returns ``(raw, exists, start_line, end_line)``.  *start_line*
    and *end_line* are ``None`` when no line range was cited or when
    the file does not exist (so the range cannot be verified).
    """
    # Strip the line-range suffix for the filesystem check.
    m = _LINE_RANGE_RE.search(raw)
    if m:
        file_part = raw[: m.start()]
        start_line = int(m.group(1))
        end_line = int(m.group(2)) if m.group(2) else start_line
    else:
        file_part = raw
        start_line = None
        end_line = None

    # Normalise: strip leading ./ and trailing /
    file_part = file_part.strip().lstrip("./").rstrip("/")
    if not file_part:
        return raw, False, None, None

    candidate = repo_dir / file_part
    exists = candidate.is_file()

    if not exists or start_line is None:
        return raw, exists, start_line, end_line

    # Verify the line range is within the file's current bounds.
    try:
        line_count = _count_lines(candidate)
    except Exception:
        log.debug("freshness: cannot count lines in %s", candidate)
        return raw, exists, start_line, end_line

    if end_line > line_count:
        log.debug(
            "freshness: cited range %d-%d exceeds %d lines in %s",
            start_line,
            end_line,
            line_count,
            file_part,
        )
        return raw, False, start_line, end_line

    return raw, exists, start_line, end_line


def _count_lines(path: Path) -> int:
    """Count lines in *path* efficiently."""
    count = 0
    with open(path, "rb") as f:
        for _ in f:
            count += 1
    return count


def _looks_like_source_path(raw: str) -> bool:
    """Return True when *raw* ends with a known source extension.

    Accepts both bare extensions (``foo.py``) and extensions followed
    by a line-range suffix (``foo.py:42``).
    """
    return any(
        raw.endswith("." + ext) or f".{ext}:" in raw for ext in _SOURCE_EXTENSIONS
    )


def _path_base(raw: str) -> str:
    """Strip the line-range suffix from *raw* for dedup purposes."""
    m = _LINE_RANGE_RE.search(raw)
    return raw[: m.start()] if m else raw


def _scan_backtick_paths(draft: str) -> list[str]:
    """Return backtick-quoted strings from *draft* that look like file paths."""
    out: list[str] = []
    for m in _BACKTICK_RE.finditer(draft):
        raw = m.group(1).strip()
        # Must contain a directory separator and a known extension.
        if "/" not in raw or not _looks_like_source_path(raw):
            continue
        out.append(raw)
    return out


def _scan_bare_paths(draft: str) -> list[str]:
    """Return bare (non-backtick) file-path-looking strings from *draft*."""
    out: list[str] = []
    for m in _BARE_PATH_RE.finditer(draft):
        raw = m.group(1).strip()
        if "/" not in raw:
            continue
        out.append(raw)
    return out


def extract_cited_paths(draft: str) -> list[str]:
    """Extract cited file-like paths from *draft*.

    Returns a de-duplicated list of raw path strings (which may
    include trailing ``:NN`` or ``:NN-NN`` line-range suffixes).
    When both a bare path and a line-range citation refer to the
    same file, only the more specific form (with line range) is kept.
    """
    seen: set[str] = set()  # raw strings already emitted
    seen_base: set[str] = set()  # base paths (without line range) already covered
    paths: list[str] = []

    # Priority 1: backtick-quoted file paths.
    # Priority 2: bare paths.  Concatenating preserves the
    # priority ordering used by the dedup logic below.
    candidates = _scan_backtick_paths(draft) + _scan_bare_paths(draft)
    for raw in candidates:
        if raw in seen:
            continue
        base = _path_base(raw)
        if base in seen_base:
            # A more specific form (with line range) already covers this.
            continue
        seen.add(raw)
        seen_base.add(base)
        paths.append(raw)

    return paths


def run_freshness_check(
    *,
    draft: str,
    repo_dir: Path | None,
) -> dict:
    """Verify that cited file paths exist on HEAD.

    Returns a dict with keys ``stale`` (bool) and ``reason`` (str).
    When ``stale`` is True the ticket should be short-circuited to
    DONE — the cited evidence no longer matches HEAD.

    Degrades gracefully: on any error returns ``stale=False`` so the
    pipeline is never blocked by the freshness gate itself.
    """
    if repo_dir is None:
        return {"stale": False, "reason": "no repo — cannot verify freshness"}

    try:
        cited = extract_cited_paths(draft)
    except Exception:
        log.warning("freshness: path extraction failed", exc_info=True)
        return {"stale": False, "reason": "extraction failed"}

    if len(cited) < 3:
        # Too few cited paths to make a reliable staleness call.
        # A single missing path is more likely a typo than a stale
        # finding.
        return {
            "stale": False,
            "reason": f"only {len(cited)} cited path(s) — insufficient for staleness check",
        }

    verified = 0
    missing: list[str] = []
    for raw in cited:
        try:
            _, exists, _, _ = _resolve_path(raw, repo_dir)
        except Exception:
            log.debug("freshness: resolution failed for %s", raw)
            exists = False
        if exists:
            verified += 1
        else:
            missing.append(raw)

    total = len(cited)
    fraction = verified / total if total > 0 else 0.0

    # Staleness thresholds (conservative — only flag when the
    # evidence is overwhelmingly missing):
    #   - 0% of cited paths exist → definite hallucination / staleness
    #   - < 33% exist and ≥ 5 cited → likely staleness
    if verified == 0 and total >= 3:
        return {
            "stale": True,
            "reason": (
                f"none of {total} cited file paths exist on HEAD"
                f" — finding is stale or hallucinated"
            ),
        }
    if fraction < 0.33 and total >= 5:
        return {
            "stale": True,
            "reason": (
                f"only {verified}/{total} cited file paths exist on HEAD"
                f" ({fraction:.0%}) — finding is likely stale"
            ),
        }

    return {
        "stale": False,
        "reason": (
            f"{verified}/{total} cited paths verified on HEAD"
            + (f" (missing: {', '.join(missing[:3])})" if missing else "")
        ),
    }
