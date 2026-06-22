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
from typing import Any

from ...config import Settings

from .filing import _build_finding
from .growth import (
    _enumerate_boards,
    _EXPLAINED_SUPPRESS_FRACTION,
    _is_meta_clone_cache_path,
    _is_periodic_pass_workspace_path,
    _TICKET_ID_PREFIX_RE,
    _workspace_ticket_states,
)
from .orphans import _CLONE_SUBDIRS

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
    suppressed: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return the top-N items above *threshold_bytes* from pre-computed size dicts.

    When *suppressed* is given, paths in that set are dropped from
    candidacy **before** the ``[:top_n]`` slice — this ensures
    self-healing infrastructure never crowds out genuinely-oversized
    items from the result.
    """
    drop = suppressed or set()

    results: list[dict[str, Any]] = [
        {"path": rel, "size_bytes": size, "is_directory": False}
        for rel, size in file_sizes.items()
        if size >= threshold_bytes and rel not in drop
    ]
    results.extend(
        {"path": rel, "size_bytes": size, "is_directory": True}
        for rel, size in dir_totals.items()
        if rel not in (".", "") and size >= threshold_bytes and rel not in drop
    )

    # Sort descending by size, then path for determinism
    results.sort(key=lambda r: (-r["size_bytes"], r["path"]))
    return results[:top_n]


# ---------------------------------------------------------------------------
# Self-healing path classification for oversized items
# ---------------------------------------------------------------------------


def _path_is_self_healing(
    rel_path: str,
    board_ids: set[str],
) -> bool:
    """Return ``True`` when *rel_path* (``data_dir``-relative) is a
    self-healing clone-cache or periodic-pass path.

    Strips the leading board-id segment when *rel_path* starts with a
    known board id, then delegates to the board-relative helpers
    ``_is_meta_clone_cache_path`` and
    ``_is_periodic_pass_workspace_path`` from ``growth.py``.
    Paths whose first segment is not a known board id are NOT
    self-healing.
    """
    first_seg, sep, remainder = rel_path.partition("/")
    if not sep or first_seg not in board_ids:
        return False
    return _is_meta_clone_cache_path(remainder) or _is_periodic_pass_workspace_path(
        remainder
    )


def _is_workspace_infra_path(remainder: str) -> bool:
    """Return True when board-relative *remainder* lies inside a ticket
    workspace (``workspaces/<ticket-id>/...``).

    A ticket's workspace (its repo/repos clone, uv venv, artifacts) is
    transient, GC-managed infrastructure whose size is inherent to the
    clone — it is never an *independently actionable* oversized alert,
    regardless of the ticket's state (active, blocked, or terminal).
    Genuinely-oversized standalone files (mill.db, stray logs) live
    outside ``workspaces/`` and are unaffected; truly unbounded files
    (memory ledgers, runs.json) are caught by the separate
    unbounded-collection check wherever they live.
    """
    parts = remainder.split("/")
    return (
        len(parts) >= 2
        and parts[0] == "workspaces"
        and bool(_TICKET_ID_PREFIX_RE.match(parts[1]))
    )


def _is_terminal_clone_cache_path(
    remainder: str, terminal_ticket_ids: set[str]
) -> bool:
    """Return True when *remainder* (board-relative) lies inside a
    terminal-ticket workspace's clone directory (repo/ or repos/)."""
    parts = remainder.split("/")
    if len(parts) < 3:
        return False
    if parts[0] != "workspaces":
        return False
    if not _TICKET_ID_PREFIX_RE.match(parts[1]):
        return False
    if parts[2] not in _CLONE_SUBDIRS:
        return False
    return parts[1] in terminal_ticket_ids


def _terminal_clone_ticket_ids(
    file_sizes: dict[str, int],
    settings: Settings,
    board_ids: set[str],
) -> dict[str, set[str]]:
    """Resolve the set of terminal-ticket IDs for each board by scanning
    *file_sizes* for ``<board>/workspaces/<tid>/<repo|repos>/...`` paths,
    batch-querying each board's DB, and filtering to terminal states.

    Returns an empty dict when ``data_dir_audit_prune_terminal_clones``
    is disabled.
    """
    if not settings.data_dir_audit_prune_terminal_clones:
        return {}

    candidates: defaultdict[str, set[str]] = defaultdict(set)
    for rel in file_sizes:
        parts = rel.split("/")
        if len(parts) < 4:
            continue
        if parts[0] not in board_ids:
            continue
        if parts[1] != "workspaces":
            continue
        if not _TICKET_ID_PREFIX_RE.match(parts[2]):
            continue
        if parts[3] not in _CLONE_SUBDIRS:
            continue
        candidates[parts[0]].add(parts[2])

    result: dict[str, set[str]] = {}
    for board_id, ticket_ids in candidates.items():
        states = _workspace_ticket_states(settings, board_id, ticket_ids)
        terminal = {tid for tid, s in states.items() if s == "terminal"}
        if terminal:
            result[board_id] = terminal
    return result


def _classify_terminal_clone_files(
    file_sizes: dict[str, int],
    board_ids: set[str],
    terminal_ids_by_board: dict[str, set[str]],
) -> set[str]:
    """Return the set of *file_sizes* keys that lie inside terminal-ticket
    clone directories."""
    result: set[str] = set()
    for rel in file_sizes:
        parts = rel.split("/")
        if len(parts) < 4:
            continue
        board_id = parts[0]
        if board_id not in board_ids:
            continue
        remainder = "/".join(parts[1:])
        terminal_ids = terminal_ids_by_board.get(board_id, set())
        if terminal_ids and _is_terminal_clone_cache_path(remainder, terminal_ids):
            result.add(rel)
    return result


def _self_healing_bytes_by_dir(
    self_healing_files: set[str], file_sizes: dict[str, int]
) -> dict[str, int]:
    """Sum self-healing-file bytes onto every ANCESTOR directory in one pass.

    O(files × depth) — replaces a per-directory rescan of every file
    (O(dirs × files)) that pegged a CPU core on large .data trees.
    """
    by_dir: dict[str, int] = defaultdict(int)
    for rel in self_healing_files:
        size = file_sizes.get(rel, 0)
        if not size:
            continue
        idx = rel.find("/")
        while idx != -1:
            by_dir[rel[:idx]] += size
            idx = rel.find("/", idx + 1)
    return by_dir


def _self_healing_oversized_paths(
    file_sizes: dict[str, int],
    dir_totals: dict[str, int],
    settings: Settings,
) -> set[str]:
    """Compute the set of oversized-path keys to suppress.

    Keys are ``data_dir``-relative paths whose bytes are self-healing
    clone-cache or periodic-pass infrastructure.

    * A **file** is suppressed when its board-relative remainder is
      classified self-healing by ``_is_meta_clone_cache_path`` or
      ``_is_periodic_pass_workspace_path``.
    * A **directory** is suppressed when ``>=90%`` of its cumulative
      bytes come from self-healing files beneath it (reusing
      ``_EXPLAINED_SUPPRESS_FRACTION`` from ``growth.py``).

    Each suppression is INFO-logged.
    """
    board_ids = {b for b in _enumerate_boards(settings)}
    if not board_ids:
        return set()

    # Classify every file
    self_healing_files: set[str] = set()
    for rel in file_sizes:
        if _path_is_self_healing(rel, board_ids):
            self_healing_files.add(rel)

    # Classify terminal-ticket clone paths (gated on the GC knob).
    terminal_ids_by_board = _terminal_clone_ticket_ids(file_sizes, settings, board_ids)
    terminal_clone_files = _classify_terminal_clone_files(
        file_sizes, board_ids, terminal_ids_by_board
    )
    self_healing_files |= terminal_clone_files

    # Classify ALL ticket-workspace files as self-healing — a workspace's
    # size is transient, GC-managed infra and is never an independently
    # actionable oversized alert (active, blocked, or terminal alike). This
    # subsumes the terminal-clone case above for the oversized check and
    # stops the recurring "oversized <board>/workspaces/<ticket>" noise.
    workspace_infra_files = _classify_workspace_infra_files(file_sizes, board_ids)
    self_healing_files |= workspace_infra_files

    suppressed: set[str] = set(self_healing_files)

    # Aggregate roots are never independently actionable: a board root and
    # its ``workspaces/`` dir are rollups of transient per-ticket workspaces
    # plus GC-managed caches. "oversized <board>" / "oversized
    # <board>/workspaces" tickets give an operator nothing to act on — the
    # specific large FILES beneath them still surface individually, and
    # growth/orphan/clone GC handle the real reclamation. Suppress the
    # rollups outright.
    aggregate_roots: set[str] = set()
    for board_id in board_ids:
        aggregate_roots.add(board_id)
        aggregate_roots.add(f"{board_id}/workspaces")
    suppressed |= aggregate_roots

    # Classify directories: suppress when >= _EXPLAINED_SUPPRESS_FRACTION
    # of cumulative bytes come from self-healing files beneath them.
    #
    # Aggregate self-healing bytes onto every ANCESTOR directory in ONE pass
    # over the self-healing files (O(files × depth)), instead of, for each
    # directory, re-scanning every file (O(dirs × files)). On a large .data
    # tree (thousands of dirs × tens of thousands of clone files) the
    # nested-loop form pegged a CPU core for the whole audit pass, holding
    # the GIL and starving the HTTP event loop — the board went unresponsive
    # on every audit run.
    self_healing_by_dir = _self_healing_bytes_by_dir(self_healing_files, file_sizes)
    for dir_key, total_size in dir_totals.items():
        if dir_key in (".", "") or total_size == 0:
            continue
        if (
            self_healing_by_dir.get(dir_key, 0) / total_size
            >= _EXPLAINED_SUPPRESS_FRACTION
        ):
            suppressed.add(dir_key)

    _log_oversized_suppressions(
        suppressed,
        file_sizes,
        dir_totals,
        terminal_clone_files=terminal_clone_files,
        aggregate_roots=aggregate_roots,
        workspace_infra_files=workspace_infra_files,
        self_healing_files=self_healing_files,
    )
    return suppressed


def _classify_workspace_infra_files(
    file_sizes: dict[str, int], board_ids: set[str]
) -> set[str]:
    """Return the *file_sizes* keys that live inside any board's ticket
    workspace (``<board>/workspaces/<ticket-id>/...``)."""
    result: set[str] = set()
    for rel in file_sizes:
        first, sep, remainder = rel.partition("/")
        if sep and first in board_ids and _is_workspace_infra_path(remainder):
            result.add(rel)
    return result


def _log_oversized_suppressions(
    suppressed: set[str],
    file_sizes: dict[str, int],
    dir_totals: dict[str, int],
    *,
    terminal_clone_files: set[str],
    aggregate_roots: set[str],
    workspace_infra_files: set[str],
    self_healing_files: set[str],
) -> None:
    """INFO-log each suppressed oversized-path key with its reason."""
    reasons = (
        (terminal_clone_files, "terminal-ticket clone cache — GC reclaims"),
        (
            aggregate_roots,
            "board/workspaces aggregate root — not independently actionable",
        ),
        (
            workspace_infra_files,
            "transient ticket-workspace infrastructure — GC-managed",
        ),
        (
            self_healing_files,
            "self-healing clone cache / periodic-pass infrastructure",
        ),
    )
    for key in sorted(suppressed):
        size = file_sizes.get(key, dir_totals.get(key, 0))
        reason = next((msg for group, msg in reasons if key in group), None)
        if reason is None:
            reason = (
                f">={_EXPLAINED_SUPPRESS_FRACTION * 100:.0f}% self-healing "
                "clone cache / periodic-pass children"
            )
        log.info(
            "data_dir_audit: suppressing oversized item path=%s size=%dB (%s)",
            key,
            size,
            reason,
        )


def find_largest_items(
    data_dir: Path,
    top_n: int = 10,
    threshold_bytes: int = 100 * 1024 * 1024,
) -> list[dict[str, Any]]:
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
_UNBOUNDED_PATTERNS: list[dict[str, str]] = [
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


_EMBED_FULL_THRESHOLD = 20 * 1024  # 20 KB


def _is_char_capped(pattern: str) -> bool:
    """Return ``True`` only for ``*_memory.md``, whose cap is measured in
    **characters** rather than bytes."""
    return pattern == "*_memory.md"


def _capture_file_content(path: Path) -> tuple[str, bool]:
    """Return ``(content_string, was_truncated)`` for embedding in a ticket body.

    * Small text files (≤ 20 KB, valid UTF-8) → full content.
    * Large text files (> 20 KB, valid UTF-8) → head (first 50 lines)
      + tail (last 50 lines) with a truncation marker.
    * Binary / undecodable → placeholder note.
    * On any read failure → ``("", False)``.
    """
    try:
        file_size = path.stat().st_size
    except OSError:
        return ("", False)

    try:
        raw = path.read_bytes()
    except OSError:
        return ("", False)

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return (
            f"_(Binary / non-UTF-8 content — {file_size} bytes on disk, "
            "excerpt not shown.)_\n",
            True,
        )

    if file_size <= _EMBED_FULL_THRESHOLD:
        return (text, False)

    # Large text file — head + tail excerpt
    lines = text.splitlines(keepends=True)
    if len(lines) <= 100:
        return (text, False)

    head_lines = lines[:50]
    tail_lines = lines[-50:]
    head_text = "".join(head_lines)
    tail_text = "".join(tail_lines)
    omitted = (
        file_size - len(head_text.encode("utf-8")) - len(tail_text.encode("utf-8"))
    )
    omitted = max(0, omitted)
    marker = f"\n… [{omitted} bytes omitted] …\n"
    return (head_text + marker + tail_text, True)


def _evaluate_path(
    path: Path,
    data_dir: Path,
    pattern: str,
    cap_bytes: int,
    cap_detail: str,
) -> dict[str, Any] | None:
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

    char_capped = _is_char_capped(pattern)
    measured_value: int
    measured_unit: str

    if char_capped:
        # Character-capped: read the file as UTF-8 text and compare
        # its character length against the cap, with a tolerance to
        # avoid flagging transient post-write overages.
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            log.debug("Could not read %s — skipping: %s", path, exc)
            return None
        measured = len(text)
        tolerance = max(round(cap_bytes * 0.02), 200)
        if measured <= cap_bytes + tolerance:
            return None
        measured_value = measured
        measured_unit = "chars"
    else:
        # Byte-capped: use st_size exactly as before.
        if size <= cap_bytes and record_count is None:
            return None
        measured_value = size
        measured_unit = "bytes"

    # Capture file content for embedding in the ticket body.
    embedded_content, content_truncated = _capture_file_content(path)

    return _build_finding(
        path,
        data_dir,
        size,
        cap_bytes,
        cap_detail,
        pattern,
        record_count,
        record_max,
        measured_value=measured_value,
        measured_unit=measured_unit,
        embedded_content=embedded_content,
        content_truncated=content_truncated,
    )


def check_unbounded_candidates(
    data_dir: Path,
    settings: Settings,
) -> list[dict[str, Any]]:
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

    findings: list[dict[str, Any]] = []
    matched: set[Path] = set()

    for entry in _UNBOUNDED_PATTERNS:
        pattern = entry["pattern"]
        glob = entry["glob"]
        cap_bytes, cap_detail = _resolve_cap(pattern, settings)

        for path in sorted(data_dir.rglob(glob)):
            if not path.is_file() or path in matched:
                continue
            # The audit's own state file is self-managed and should
            # never be flagged as an unbounded collection.
            if path.name == "data_dir_audit_state.json":
                continue
            matched.add(path)

            finding = _evaluate_path(path, data_dir, pattern, cap_bytes, cap_detail)
            if finding is not None:
                findings.append(finding)

    return findings
