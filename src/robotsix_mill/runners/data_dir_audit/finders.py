"""Top-N largest-items detection and unbounded-collection checks (tickets 2, 4).

Includes ``find_largest_items`` for oversized file/directory detection
and ``check_unbounded_candidates`` for files exceeding known caps.
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from pathlib import Path

from ...config import Settings

from .filing import _build_finding

log = logging.getLogger("robotsix_mill.data_dir_audit")


# ---------------------------------------------------------------------------
#  Top-N largest items detection (ticket 2)
# ---------------------------------------------------------------------------


def _file_size_or_none(fp: str) -> int | None:
    """Return ``os.path.getsize(fp)``, or ``None`` on OSError (logged)."""
    try:
        return os.path.getsize(fp)
    except OSError as err:
        log.warning("Cannot access %s: %s", fp, err)
        return None


def _accumulate_ancestors(
    fp: str, data_dir: Path, size: int, dir_totals: defaultdict[str, int]
) -> None:
    """Add *size* to every ancestor of *fp* up to (but excluding) *data_dir*."""
    ancestor = os.path.dirname(fp)
    while ancestor != str(data_dir):
        parent_rel = os.path.relpath(ancestor, data_dir)
        dir_totals[parent_rel] += size
        ancestor = os.path.dirname(ancestor)


def _collect_sizes(
    data_dir: Path,
) -> tuple[dict[str, int], defaultdict[str, int]]:
    """Walk *data_dir* and return (file_sizes, dir_totals)."""
    file_sizes: dict[str, int] = {}
    dir_totals: defaultdict[str, int] = defaultdict(int)

    for dirpath_str, _dirnames, filenames in os.walk(data_dir, followlinks=False):
        for fname in filenames:
            fp = os.path.join(dirpath_str, fname)
            if os.path.islink(fp):
                continue
            size = _file_size_or_none(fp)
            if size is None or size == 0:
                continue
            rel = os.path.relpath(fp, data_dir)
            file_sizes[rel] = size
            _accumulate_ancestors(fp, data_dir, size, dir_totals)

    return file_sizes, dir_totals


def _select_largest_from_sizes(
    file_sizes: dict[str, int],
    dir_totals: defaultdict[str, int],
    top_n: int,
    threshold_bytes: int,
) -> list[dict]:
    """Return the top-N items above *threshold_bytes* from pre-computed size dicts."""
    results: list[dict] = [
        {"path": rel, "size_bytes": size, "is_directory": False}
        for rel, size in file_sizes.items()
        if size >= threshold_bytes
    ]
    results.extend(
        {"path": rel, "size_bytes": size, "is_directory": True}
        for rel, size in dir_totals.items()
        if rel not in (".", "") and size >= threshold_bytes
    )

    # Sort descending by size, then path for determinism
    results.sort(key=lambda r: (-r["size_bytes"], r["path"]))
    return results[:top_n]


def find_largest_items(
    data_dir: Path,
    top_n: int = 10,
    threshold_bytes: int = 100 * 1024 * 1024,
) -> list[dict]:
    """Return top-N items under *data_dir* whose size ≥ *threshold_bytes*.

    Returns a list of dicts, each with keys ``path`` (str, relative to
    *data_dir*), ``size_bytes`` (int), and ``is_directory`` (bool).
    """
    if not data_dir.is_dir():
        return []

    file_sizes, dir_totals = _collect_sizes(data_dir)
    return _select_largest_from_sizes(file_sizes, dir_totals, top_n, threshold_bytes)


# ---------------------------------------------------------------------------
#  Unbounded-collection candidate detection (ticket 4)
# ---------------------------------------------------------------------------

# Hardcoded byte caps for known unbounded patterns. The spec sources
# `*_memory.md` from ``settings.max_memory_chars``; the rest are
# hardcoded defaults because no corresponding ``Settings`` fields
# exist (see ticket spec — caps for ci_patterns.json /
# ci_monitor_state.json / generic JSON are tunable only via a future
# settings expansion).
_RUNS_JSON_CAP_BYTES = 25 * 1024  # ~25 KB (MAX_ENTRIES=50 × ~500 B)
_RUNS_JSON_MAX_ENTRIES = 50
_CI_PATTERNS_CAP_BYTES = 1024 * 1024  # 1 MB
_CI_MONITOR_STATE_CAP_BYTES = 500 * 1024  # 500 KB
_GENERIC_JSON_CAP_BYTES = 5 * 1024 * 1024  # 5 MB


# Module-level pattern registry. Order matters: the specific patterns
# come first, generic ``*.json`` is the fall-through. Files already
# matched by an earlier specific pattern are excluded from later
# patterns to avoid double-flagging.
_UNBOUNDED_PATTERNS: list[dict] = [
    {"pattern": "*_memory.md", "glob": "*_memory.md"},
    {"pattern": "runs.json", "glob": "runs.json"},
    {"pattern": "ci_patterns.json", "glob": "ci_patterns.json"},
    {"pattern": "ci_monitor_state.json", "glob": "ci_monitor_state.json"},
    {"pattern": "*.json", "glob": "*.json"},
]


def _resolve_cap(pattern: str, settings: Settings) -> tuple[int, str]:
    """Return ``(cap_bytes, cap_detail)`` for the given pattern name."""
    if pattern == "*_memory.md":
        cap = settings.max_memory_chars
        return cap, f"max_memory_chars={cap}"
    if pattern == "runs.json":
        return _RUNS_JSON_CAP_BYTES, f"MAX_ENTRIES={_RUNS_JSON_MAX_ENTRIES} (~25 KB)"
    if pattern == "ci_patterns.json":
        return _CI_PATTERNS_CAP_BYTES, "default=1 MB"
    if pattern == "ci_monitor_state.json":
        return _CI_MONITOR_STATE_CAP_BYTES, "default=500 KB"
    if pattern == "*.json":
        return _GENERIC_JSON_CAP_BYTES, "default=5 MB"
    raise ValueError(f"Unknown unbounded pattern: {pattern!r}")


def _count_runs_json_entries(path: Path) -> tuple[int | None, int | None]:
    """Return ``(record_count, record_max)`` for a ``runs.json`` file.

    Both values are ``None`` if the file parses cleanly but is within
    the entry-count cap, or if the JSON is unparseable. Parse errors
    are silently logged at debug level — the size check still applies
    at the caller.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.debug(
            "Could not parse %s — skipping record-count check: %s",
            path,
            exc,
        )
        return None, None
    if isinstance(data, list) and len(data) > _RUNS_JSON_MAX_ENTRIES:
        return len(data), _RUNS_JSON_MAX_ENTRIES
    return None, None


def _evaluate_path(
    path: Path,
    data_dir: Path,
    pattern: str,
    cap_bytes: int,
    cap_detail: str,
) -> dict | None:
    """Return a finding dict for ``path`` if it exceeds its cap, else None."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        log.debug("Could not stat %s — skipping: %s", path, exc)
        return None

    record_count: int | None = None
    record_max: int | None = None
    if pattern == "runs.json":
        record_count, record_max = _count_runs_json_entries(path)

    if size <= cap_bytes and record_count is None:
        return None

    return _build_finding(
        path,
        data_dir,
        size,
        cap_bytes,
        cap_detail,
        pattern,
        record_count,
        record_max,
    )


def check_unbounded_candidates(
    data_dir: Path,
    settings: Settings,
) -> list[dict]:
    """Inspect ``data_dir`` for files exceeding known unbounded-pattern caps.

    Walks ``data_dir`` recursively, applies the specific-pattern globs
    first (``*_memory.md``, ``runs.json``, ``ci_patterns.json``,
    ``ci_monitor_state.json``), then a generic ``*.json`` glob to any
    remaining JSON files. For each match whose size exceeds the
    documented cap — or, for ``runs.json``, whose top-level array
    length exceeds ``MAX_ENTRIES`` (50) — a finding dict is produced.

    Pure inspection: no state is mutated. Corrupt/unparseable JSON is
    silently skipped with a debug-level log entry.

    Args:
        data_dir: Root directory to walk (typically ``settings.data_dir``).
        settings: Loaded :class:`Settings` (only ``max_memory_chars`` is
            consulted for the memory-ledger cap).

    Returns:
        A list of finding dicts; empty when nothing exceeds its cap.
    """
    if not data_dir.exists():
        return []

    findings: list[dict] = []
    matched: set[Path] = set()

    for entry in _UNBOUNDED_PATTERNS:
        pattern = entry["pattern"]
        glob = entry["glob"]
        cap_bytes, cap_detail = _resolve_cap(pattern, settings)

        for path in sorted(data_dir.rglob(glob)):
            if not path.is_file() or path in matched:
                continue
            matched.add(path)

            finding = _evaluate_path(path, data_dir, pattern, cap_bytes, cap_detail)
            if finding is not None:
                findings.append(finding)

    return findings
