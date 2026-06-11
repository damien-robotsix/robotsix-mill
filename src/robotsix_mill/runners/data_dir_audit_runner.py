"""Data-dir audit runner — periodic survey of ``.data/`` monotonic growth.

Inspection checks (top-N largest items, growth deltas, unbounded-
collection candidates, orphan workspaces, ticket filing & dedup, rich
summary) are added by child tickets 2–7 of the epic.

This module currently implements:

- Ticket 2: top-N largest-items detection (:func:`find_largest_items`).
- Ticket 3: growth-delta tracking with persistent prior-pass state
  (:func:`_growth_state_path`, :func:`_load_growth_state`,
  :func:`_save_growth_state`, :func:`_enumerate_boards`,
  :func:`_scan_board_sizes`, :func:`_compute_growth_deltas`).
- Ticket 4: unbounded-collection candidate detection
  (:func:`check_unbounded_candidates`).
- Ticket 5: orphan-workspace detection
  (:func:`find_orphan_workspaces`).
- Ticket 6: filing logic with cross-pass dedup via gap-id markers
  (:func:`_file_findings_as_tickets`).

Seam: tests monkeypatch ``robotsix_mill.data_dir_audit_runner.Settings``
to inject fake settings instances. The :class:`Settings` import below
is kept precisely to expose that attribute on the module namespace for
monkeypatching.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import select

from ..config import RepoConfig, Settings
from ..core import db
from ..core.models import SourceKind, Ticket, TicketEvent, _now
from ..core.service import TicketService
from ..core.states import State

log = logging.getLogger("robotsix_mill.data_dir_audit")

# Lenient ticket-ID prefix check: only the leading timestamp is
# validated (``YYYYmmddTHHMMSSZ-``). The suffix is left unparsed so
# future ID-format changes do not produce false negatives, while
# obviously non-ticket directories (``.gitkeep``, ``artifacts``, …)
# are still filtered out.
_TICKET_ID_PREFIX_RE = re.compile(r"^\d{8}T\d{6}Z-")

# Maximum number of ticket IDs per ``SELECT … WHERE id IN (…)`` batch.
# Keeps the IN-clause small enough for SQLite while still avoiding the
# one-query-per-directory anti-pattern.
_BATCH_SIZE = 500

# Terminal ticket states: those with empty outgoing transition sets in
# ``core.states`` (``TRANSITIONS[...] == set()``). A workspace whose
# ticket sits in one of these is eligible for prune_closed GC.
_TERMINAL_STATES = {State.CLOSED, State.EPIC_CLOSED, State.ANSWERED}


@dataclass
class OrphanWorkspace:
    """A workspace directory whose ticket no longer exists in the DB."""

    board_id: str
    ticket_id: str
    path: Path
    dir_size_bytes: int


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


def _build_finding(
    path: Path,
    data_dir: Path,
    size: int,
    cap_bytes: int,
    cap_detail: str,
    pattern: str,
    record_count: int | None,
    record_max: int | None,
) -> dict:
    """Build a finding dict for ``path`` against its pattern's caps."""
    try:
        rel_path = str(path.relative_to(data_dir))
    except ValueError:
        rel_path = str(path)
    return {
        "check": "unbounded_candidates",
        "path": rel_path,
        "current_size": size,
        "cap_size": cap_bytes,
        "cap_detail": cap_detail,
        "pattern": pattern,
        "severity": "warning",
        "record_count": record_count,
        "record_max": record_max,
    }


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


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class DataDirAuditPassResult:
    """Result of running a data-dir audit pass."""

    drafts_created: list[dict]  # [{"id": ..., "title": ...}]
    summary: str
    oversized_items: list[dict] = field(default_factory=list)
    # [{"path": "relative/path", "size_bytes": 123456, "is_directory": false}, …]
    updated_memory: str = ""
    session_id: str = ""
    findings: list[dict] = field(default_factory=list)
    growth_flags: list[dict] = field(default_factory=list)
    # Number of terminal-state ticket workspaces removed by the opt-in
    # prune_closed GC step (0 when the knob is disabled).
    closed_pruned: int = 0


# ---------------------------------------------------------------------------
# State file helpers (ticket 3: growth-delta tracking)
# ---------------------------------------------------------------------------


def _growth_state_path(settings: Settings, board_id: str) -> Path:
    """Return the per-board persistent state file path.

    Matches the board-scoped pattern used by
    :func:`trace_review_runner._state_path`.
    """
    return settings.data_dir / board_id / "data_dir_audit_state.json"


def _load_growth_state(state_path: Path) -> dict[str, dict]:
    """Load prior-pass size state from *state_path*.

    Returns an empty dict on first-run (file absent) or when the file
    is corrupt/unreadable — mirroring the pattern in
    :func:`trace_review_runner._load_watermark`.
    """
    if not state_path.exists():
        return {}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            # Defensive: ensure every value is a dict with expected keys.
            # Keys with non-dict values are dropped (migration safety).
            return {
                k: v
                for k, v in data.items()
                if isinstance(v, dict) and "size_bytes" in v and "mtime" in v
            }
        return {}
    except Exception:  # noqa: BLE001 — corrupt state = first-run
        log.warning("data_dir_audit_state.json unreadable at %s — ignoring", state_path)
        return {}


def _save_growth_state(state_path: Path, state: dict[str, dict]) -> None:
    """Atomically persist *state* to *state_path*.

    Writes to a ``.json.tmp`` sibling first, then replaces the target
    — following the pattern in :func:`agents.candidates.update_status`.
    """
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(state_path)


# ---------------------------------------------------------------------------
# Board enumeration
# ---------------------------------------------------------------------------


def _enumerate_boards(settings: Settings) -> list[str]:
    """Return board IDs for every board directory under ``.data/``.

    Scans ``settings.data_dir`` for subdirectories containing
    ``mill.db``. Mirrors the pattern used in
    :func:`timeout_escalation_runner._boards_to_scan` and
    :func:`verify_runner` (inline).
    """
    boards: list[str] = []
    try:
        for child in sorted(settings.data_dir.iterdir()):
            if child.is_dir() and (child / "mill.db").exists():
                boards.append(child.name)
    except OSError:
        log.exception("data-dir audit: failed to enumerate boards")
    return boards


# ---------------------------------------------------------------------------
# Size scan
# ---------------------------------------------------------------------------


def _record_entry(entry: Path, board_dir: Path, result: dict[str, dict]) -> None:
    """Add a single filesystem *entry* to *result* if it qualifies.

    Symlinks, unreadable entries, and the audit state file itself are
    skipped silently.
    """
    if entry.is_symlink():
        return
    try:
        stat = entry.stat()
    except OSError:
        return
    rel = entry.relative_to(board_dir).as_posix()
    if rel == "data_dir_audit_state.json":
        return
    if entry.is_file():
        result[rel] = {"size_bytes": stat.st_size, "mtime": stat.st_mtime}
    elif entry.is_dir():
        # Append trailing '/' so directory keys are distinguishable
        # from file keys when computing cumulative sizes.
        result[rel + "/"] = {"size_bytes": 0, "mtime": stat.st_mtime}


def _compute_cumulative_dir_sizes(result: dict[str, dict]) -> None:
    """Fill in cumulative directory sizes in *result* (in place).

    For each directory key, sum the sizes of all files whose path
    starts with that directory prefix. Sort deepest-first so parent
    directories naturally include their subdirectory contents. Only
    file entries (keys not ending with "/") are summed — each file is
    counted once per containing directory.
    """
    dir_keys = {k for k in result if k.endswith("/")}
    for dir_key in sorted(dir_keys, key=len, reverse=True):
        cumulative = 0
        for path_key, info in result.items():
            if path_key == dir_key or not path_key.startswith(dir_key):
                continue
            if not path_key.endswith("/"):
                cumulative += info["size_bytes"]
        result[dir_key]["size_bytes"] = cumulative


def _scan_board_sizes(board_dir: Path) -> dict[str, dict]:
    """Walk *board_dir* and record file sizes + cumulative directory sizes.

    Returns a dict mapping POSIX relative paths to
    ``{"size_bytes": int, "mtime": float}``.

    - Files: ``st_size`` + ``st_mtime``.
    - Directories: cumulative size of all files under them (recursive)
      + the directory's own ``st_mtime``.
    - Symlinks are skipped entirely (not followed, not measured).
    - The state file itself (``data_dir_audit_state.json``) is excluded.
    """
    result: dict[str, dict] = {}
    # First pass: collect all file entries (skipping symlinks + state file).
    for entry in board_dir.rglob("*"):
        _record_entry(entry, board_dir, result)

    # Second pass: compute cumulative directory sizes.
    _compute_cumulative_dir_sizes(result)

    return result


# ---------------------------------------------------------------------------
# Growth-delta computation
# ---------------------------------------------------------------------------


def _compute_growth_deltas(
    prior: dict[str, dict],
    current: dict[str, dict],
    settings: Settings,
    board_id: str = "",
) -> list[dict]:
    """Compare *prior* and *current* size snapshots; flag excessive growth.

    For every path present in *both* snapshots:
    - Compute ``delta_bytes = current_size - prior_size``.
    - Skip if ``delta_bytes <= 0`` (shrank or unchanged).
    - Compute ``delta_pct``; guard against division by zero.
    - Flag if ``delta_bytes >= growth_delta_bytes`` **OR**
      ``delta_pct >= growth_delta_pct``.

    Returns a list of flag-dicts (empty if nothing flagged).
    """
    flags: list[dict] = []
    threshold_bytes = settings.data_dir_audit_growth_delta_bytes
    threshold_pct = settings.data_dir_audit_growth_delta_pct

    for path_key in prior:
        if path_key not in current:
            continue  # path was deleted — pruned on save
        prior_info = prior[path_key]
        current_info = current[path_key]
        delta_bytes = current_info["size_bytes"] - prior_info["size_bytes"]
        if delta_bytes <= 0:
            continue

        prior_size = prior_info["size_bytes"]
        if prior_size == 0:
            delta_pct = 100.0 if delta_bytes > 0 else 0.0
        else:
            delta_pct = (delta_bytes / prior_size) * 100

        exceeded: list[str] = []
        if delta_bytes >= threshold_bytes:
            exceeded.append("bytes")
        if delta_pct >= threshold_pct:
            exceeded.append("pct")
        if not exceeded:
            continue

        flags.append(
            {
                "check": "growth_delta",
                "path": path_key,
                "board_id": board_id,
                "current_size_bytes": current_info["size_bytes"],
                "prior_size_bytes": prior_info["size_bytes"],
                "delta_bytes": delta_bytes,
                "delta_pct": round(delta_pct, 1),
                "threshold_exceeded": "both" if len(exceeded) == 2 else exceeded[0],
            }
        )
    return flags


# ---------------------------------------------------------------------------
# Orphan-workspace detection (ticket 5)
# ---------------------------------------------------------------------------


def _dir_size_bytes(path: Path) -> int:
    """Approximate on-disk size of *path*.

    Sums ``stat().st_size`` for every regular file under *path* via
    ``rglob``. No deduplication for hardlinks and no filesystem-level
    block accounting — acceptable for a detection heuristic
    (per the ticket spec).
    """
    total = 0
    try:
        for child in path.rglob("*"):
            try:
                if child.is_file():
                    total += child.stat().st_size
            except OSError:
                # Skip files that vanish between rglob and stat.
                continue
    except OSError:
        return total
    return total


def _boards_from_disk(settings: Settings) -> list[str]:
    """Return board IDs that have a ``mill.db`` on disk.

    Mirrors the pattern from ``verify_runner`` and
    ``timeout_escalation_runner``: only boards that have actually been
    materialised on disk are scanned. Registered-but-not-yet-created
    boards are skipped because they have no DB to cross-reference.
    """
    boards: list[str] = []
    try:
        for child in sorted(settings.data_dir.iterdir()):
            if child.is_dir() and (child / "mill.db").exists():
                boards.append(child.name)
    except OSError:
        pass
    return boards


def find_orphan_workspaces(
    settings: Settings,
    board_id: str,
) -> list[OrphanWorkspace]:
    """Return workspace directories whose ticket no longer exists.

    Lists every subdirectory under ``<data_dir>/<board_id>/workspaces/``,
    cross-references the names against *board_id*'s ``mill.db`` in one
    batched ``SELECT … WHERE id IN (…)`` per batch (batch size
    ``≤ 500``), and returns an :class:`OrphanWorkspace` for each
    directory whose ticket ID is absent from the DB.

    The function is board-scoped: a workspace directory in board ``A``
    is never compared against board ``B``'s DB.

    Returns an empty list when the workspaces directory does not
    exist (e.g. fresh board with zero tickets), when it is empty, or
    when every subdirectory corresponds to a live ticket.

    Subdirectories whose name does not match the ticket-ID timestamp
    prefix (``^\\d{8}T\\d{6}Z-``) are skipped with a ``WARNING`` log
    rather than counted as orphans — this filters obviously-non-ticket
    entries like ``.gitkeep`` or ``artifacts`` without crashing.
    """
    workspaces_dir = settings.workspaces_dir_for(board_id)
    if not workspaces_dir.exists():
        return []

    candidates: list[tuple[str, Path]] = []
    try:
        for child in sorted(workspaces_dir.iterdir()):
            if not child.is_dir():
                continue
            name = child.name
            if not _TICKET_ID_PREFIX_RE.match(name):
                log.warning(
                    "data_dir_audit: board=%r — skipping non-ticket-ID "
                    "directory %r in workspaces/",
                    board_id,
                    name,
                )
                continue
            candidates.append((name, child))
    except OSError:
        return []

    if not candidates:
        return []

    # Batched DB cross-reference: collect the set of IDs that exist
    # in the DB, then diff against the on-disk candidates.
    candidate_ids = [name for name, _ in candidates]
    existing_ids: set[str] = set()
    with db.session(settings, board_id) as s:
        for start in range(0, len(candidate_ids), _BATCH_SIZE):
            chunk = candidate_ids[start : start + _BATCH_SIZE]
            stmt = select(Ticket.id).where(Ticket.id.in_(chunk))
            existing_ids.update(s.exec(stmt).all())

    orphans: list[OrphanWorkspace] = []
    for name, path in candidates:
        if name in existing_ids:
            continue
        orphans.append(
            OrphanWorkspace(
                board_id=board_id,
                ticket_id=name,
                path=path,
                dir_size_bytes=_dir_size_bytes(path),
            )
        )
    return orphans


# ---------------------------------------------------------------------------
# Opt-in GC: prune workspaces of terminal-state tickets
# ---------------------------------------------------------------------------


def _close_time_from_ticket_id(name: str) -> datetime | None:
    """Parse a tz-aware close time from a ticket-ID timestamp prefix.

    The prefix is ``YYYYmmddTHHMMSSZ-`` (16 chars before the dash).
    Returns ``None`` when the prefix does not parse — a defensive
    fallback used only when no terminal ``TicketEvent`` exists.
    """
    try:
        return datetime.strptime(name[:16], "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _workspace_candidates(workspaces_dir: Path) -> list[tuple[str, Path]]:
    """List ``(ticket_id, path)`` for ticket-ID-named workspace subdirs."""
    candidates: list[tuple[str, Path]] = []
    for child in sorted(workspaces_dir.iterdir()):
        if not child.is_dir():
            continue
        name = child.name
        if not _TICKET_ID_PREFIX_RE.match(name):
            continue
        candidates.append((name, child))
    return candidates


def _terminal_close_times(
    settings: Settings,
    board_id: str,
    candidate_ids: list[str],
) -> tuple[set[str], dict[str, datetime]]:
    """Cross-reference *candidate_ids* against *board_id*'s DB in batched
    ``IN`` selects.

    Returns ``(terminal_ids, close_times)`` where ``terminal_ids`` are
    the candidate IDs whose ticket exists AND is in a terminal state,
    and ``close_times`` maps each such ID to the max ``at`` of its
    terminal ``TicketEvent`` rows (the close time).
    """
    terminal_ids: set[str] = set()
    close_times: dict[str, datetime] = {}
    with db.session(settings, board_id) as s:
        for start in range(0, len(candidate_ids), _BATCH_SIZE):
            chunk = candidate_ids[start : start + _BATCH_SIZE]
            chunk_terminal = set(
                s.exec(
                    select(Ticket.id).where(
                        Ticket.id.in_(chunk),
                        Ticket.state.in_(_TERMINAL_STATES),
                    )
                ).all()
            )
            if not chunk_terminal:
                continue
            terminal_ids.update(chunk_terminal)
            rows = s.exec(
                select(TicketEvent.ticket_id, TicketEvent.at).where(
                    TicketEvent.ticket_id.in_(chunk_terminal),
                    TicketEvent.state.in_(_TERMINAL_STATES),
                )
            ).all()
            for ticket_id, at in rows:
                # Keep the most recent terminal-event time per ticket.
                prior = close_times.get(ticket_id)
                if prior is None or at > prior:
                    close_times[ticket_id] = at
    return terminal_ids, close_times


def _prune_board_workspaces(
    settings: Settings,
    board_id: str,
    now: datetime,
    age_threshold_seconds: int,
) -> int:
    """Remove terminal-state ticket workspaces for one board.

    Mirrors :func:`find_orphan_workspaces`: lists workspace subdirs,
    skips non-ticket-ID names, and cross-references the board DB in
    batched ``IN`` selects. A directory is removed only when its ticket
    is present AND in a terminal state AND its close time is at least
    *age_threshold_seconds* old. Returns the number of dirs removed.
    """
    workspaces_dir = settings.workspaces_dir_for(board_id)
    if not workspaces_dir.exists():
        return 0

    candidates = _workspace_candidates(workspaces_dir)
    if not candidates:
        return 0

    candidate_ids = [name for name, _ in candidates]
    terminal_ids, close_times = _terminal_close_times(settings, board_id, candidate_ids)

    removed = 0
    for name, path in candidates:
        if name not in terminal_ids:
            continue
        close_time = close_times.get(name) or _close_time_from_ticket_id(name)
        if close_time is None:
            continue
        if (now - close_time).total_seconds() < age_threshold_seconds:
            continue
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            removed += 1
            log.info(
                "data_dir_audit: pruned closed workspace board=%r ticket=%s path=%s",
                board_id,
                name,
                path,
            )
    if removed:
        log.info(
            "data_dir_audit: board=%r pruned %d closed workspace(s)",
            board_id,
            removed,
        )
    return removed


def _prune_closed_workspaces(settings: Settings) -> int:
    """Remove workspace dirs of terminal-state tickets older than the
    configured age. Returns the number of directories removed."""
    now = _now()
    age_threshold_seconds = settings.data_dir_audit_prune_closed_age_seconds
    total_removed = 0
    for board_id in _boards_from_disk(settings):
        try:
            total_removed += _prune_board_workspaces(
                settings, board_id, now, age_threshold_seconds
            )
        except Exception:
            log.warning(
                "data_dir_audit: board=%r — closed-workspace prune failed",
                board_id,
                exc_info=True,
            )
            continue
    return total_removed


# ---------------------------------------------------------------------------
# Filing logic (ticket 6)
# ---------------------------------------------------------------------------


_WHITESPACE_RE = re.compile(r"\s+")


def _human_bytes(n: int) -> str:
    """Return a binary-unit string for *n* bytes (``"1.2 GiB"``).

    Uses powers of 1024 to match ``.data/`` accounting conventions.
    Negative values are formatted with a leading minus sign.
    """
    if n < 0:
        return "-" + _human_bytes(-n)
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if size < 1024.0 or unit == "PiB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PiB"  # unreachable but appeases type checkers


def _trim_path(p: str, max_len: int = 80) -> str:
    """Middle-elide *p* with ``…`` so the result is at most *max_len* chars."""
    if len(p) <= max_len:
        return p
    # Keep room for the ellipsis (1 char).
    keep = max_len - 1
    head = keep // 2
    tail = keep - head
    return p[:head] + "…" + p[-tail:]


def _sanitize_gap_segment(s: str) -> str:
    """Replace any whitespace in *s* with ``_`` so the gap_id matches
    ``\\S+`` (no whitespace runs)."""
    return _WHITESPACE_RE.sub("_", s)


def _build_oversized_finding(item: dict) -> tuple[str, str, str]:
    """Return ``(gap_id, title, body)`` for an oversized-item finding."""
    path = item["path"]
    size = int(item["size_bytes"])
    is_dir = bool(item.get("is_directory"))
    gap_id = f"oversized:{_sanitize_gap_segment(path)}"
    kind = "directory" if is_dir else "file"
    title = f"oversized {path} ({_human_bytes(size)})"
    body = (
        "_Filed by the periodic data-dir audit pass._\n\n"
        "## Finding\n\n"
        f"- **Path:** `{path}` ({kind})\n"
        f"- **Current size:** {_human_bytes(size)} ({size} bytes)\n"
        "- **Threshold:** "
        f"{_human_bytes(100 * 1024 * 1024)} (default oversized threshold)\n\n"
        "Consider capping this file or scheduling a sweep.\n"
    )
    return gap_id, title, body


def _build_growth_finding(flag: dict) -> tuple[str, str, str]:
    """Return ``(gap_id, title, body)`` for a growth-delta finding."""
    path = flag["path"]
    board_id = flag.get("board_id", "")
    delta_bytes = int(flag["delta_bytes"])
    delta_pct = flag["delta_pct"]
    current_size = int(flag["current_size_bytes"])
    prior_size = int(flag["prior_size_bytes"])
    threshold_exceeded = flag.get("threshold_exceeded", "")
    gap_id = f"growth:{_sanitize_gap_segment(board_id)}:{_sanitize_gap_segment(path)}"
    title = f"growth {path} (+{_human_bytes(delta_bytes)}, +{delta_pct}%)"
    body = (
        "_Filed by the periodic data-dir audit pass._\n\n"
        "## Finding\n\n"
        f"- **Board:** `{board_id}`\n"
        f"- **Path:** `{path}`\n"
        f"- **Prior size:** {_human_bytes(prior_size)} ({prior_size} bytes)\n"
        f"- **Current size:** {_human_bytes(current_size)} "
        f"({current_size} bytes)\n"
        f"- **Delta:** +{_human_bytes(delta_bytes)} ({delta_bytes} bytes), "
        f"+{delta_pct}%\n"
        f"- **Threshold exceeded:** {threshold_exceeded}\n\n"
        f"Investigate why this path grew by {_human_bytes(delta_bytes)} "
        "since the last audit pass.\n"
    )
    return gap_id, title, body


def _build_unbounded_finding(finding: dict) -> tuple[str, str, str]:
    """Return ``(gap_id, title, body)`` for an unbounded-collection finding."""
    path = finding["path"]
    current_size = int(finding["current_size"])
    cap_bytes = int(finding["cap_size"])
    cap_detail = finding.get("cap_detail", "")
    pattern = finding.get("pattern", "")
    record_count = finding.get("record_count")
    record_max = finding.get("record_max")
    gap_id = f"unbounded:{_sanitize_gap_segment(path)}"
    title = f"unbounded {path} (>{_human_bytes(cap_bytes)})"
    body_lines = [
        "_Filed by the periodic data-dir audit pass._",
        "",
        "## Finding",
        "",
        f"- **Path:** `{path}`",
        f"- **Current size:** {_human_bytes(current_size)} ({current_size} bytes)",
        f"- **Cap:** {cap_detail} ({_human_bytes(cap_bytes)})",
        f"- **Pattern:** `{pattern}`",
    ]
    if record_count is not None and record_max is not None:
        body_lines.append(f"- **Record count:** {record_count} (max {record_max})")
    body_lines.append("")
    body_lines.append(
        f"This file exceeds its documented cap ({cap_detail}); "
        "consider pruning or capping.\n"
    )
    body = "\n".join(body_lines)
    return gap_id, title, body


def _build_orphan_finding(orphan: OrphanWorkspace) -> tuple[str, str, str]:
    """Return ``(gap_id, title, body)`` for an orphan-workspace finding."""
    board_id = orphan.board_id
    ticket_id = orphan.ticket_id
    size = orphan.dir_size_bytes
    gap_id = (
        f"orphan:{_sanitize_gap_segment(board_id)}:{_sanitize_gap_segment(ticket_id)}"
    )
    title = (
        f"data-dir audit: orphan workspace {board_id}/{ticket_id} "
        f"({_human_bytes(size)})"
    )
    body = (
        "_Filed by the periodic data-dir audit pass._\n\n"
        "## Finding\n\n"
        f"- **Board:** `{board_id}`\n"
        f"- **Ticket id:** `{ticket_id}`\n"
        f"- **Path:** `{orphan.path}`\n"
        f"- **Dir size:** {_human_bytes(size)} ({size} bytes)\n\n"
        "This workspace dir belongs to a ticket no longer in the DB; "
        "consider removing it.\n"
    )
    return gap_id, title, body


def _order_findings(
    oversized: list[dict],
    growth_flags: list[dict],
    unbounded: list[dict],
    orphans_by_board: dict[str, list[OrphanWorkspace]],
) -> list[tuple[str, str, str]]:
    """Order findings deterministically and return ``[(gap_id, title, body)]``.

    Filing priority: orphans → growth (delta_bytes desc) → oversized
    (size_bytes desc) → unbounded (current_size desc).
    """
    ordered: list[tuple[str, str, str]] = []

    # 1. Orphans: sort by board_id, then ticket_id.
    for board_id in sorted(orphans_by_board):
        for orphan in sorted(orphans_by_board[board_id], key=lambda o: o.ticket_id):
            ordered.append(_build_orphan_finding(orphan))

    # 2. Growth flags: delta_bytes desc, then path for ties.
    for flag in sorted(
        growth_flags,
        key=lambda f: (-int(f.get("delta_bytes", 0)), f.get("path", "")),
    ):
        ordered.append(_build_growth_finding(flag))

    # 3. Oversized: size_bytes desc, then path for ties.
    for item in sorted(
        oversized,
        key=lambda i: (-int(i.get("size_bytes", 0)), i.get("path", "")),
    ):
        ordered.append(_build_oversized_finding(item))

    # 4. Unbounded: current_size desc, then path for ties.
    for finding in sorted(
        unbounded,
        key=lambda f: (-int(f.get("current_size", 0)), f.get("path", "")),
    ):
        ordered.append(_build_unbounded_finding(finding))

    return ordered


def _file_findings_as_tickets(
    settings: Settings,
    service: TicketService,
    oversized: list[dict],
    growth_flags: list[dict],
    unbounded: list[dict],
    orphans_by_board: dict[str, list[OrphanWorkspace]],
    session_id: str = "",
) -> list[dict]:
    """File draft tickets for findings, dedup'd via gap-id markers.

    Returns ``[{"id": ticket.id, "title": ticket.title}, ...]`` for
    every draft actually created. Findings whose gap-id matches an
    *in-flight* prior draft are silently skipped; findings beyond
    ``settings.data_dir_audit_max_drafts_per_pass`` are dropped.

    Filing target: every draft is created on the service the caller
    passes in — typically the scheduling board's ``TicketService``.
    When the periodic audit is enabled on multiple boards, each board
    scans the entire ``.data/`` and would race to file overlapping
    findings. Cross-pass dedup catches re-runs within one board's
    history but does NOT prevent cross-board duplication on the first
    pass. Acceptable trade-off — operators are expected to enable
    ``data_dir_audit_periodic`` on a single (maintenance) board.
    """
    from .pass_runner import _verify_prior_proposals

    prior = _verify_prior_proposals(service, settings, SourceKind.DATA_DIR_AUDIT)
    in_flight: set[str] = {
        gid for gid, info in prior.items() if info["resolution"] == "in-flight"
    }

    ordered = _order_findings(oversized, growth_flags, unbounded, orphans_by_board)

    cap = settings.data_dir_audit_max_drafts_per_pass
    created: list[dict] = []
    for gap_id, title, body in ordered:
        if cap > 0 and len(created) >= cap:
            log.info(
                "data_dir_audit: hit per-pass cap of %d drafts — "
                "remaining findings dropped (will be reconsidered next pass)",
                cap,
            )
            break
        if gap_id in in_flight:
            log.debug(
                "data_dir_audit: skipping finding — in-flight ticket "
                "already filed for gap_id=%s",
                gap_id,
            )
            continue
        marker = f"<!-- data_dir_audit-gap-id: {gap_id} -->"
        body_with_marker = body.rstrip() + "\n\n" + marker
        try:
            ticket = service.create(
                title=title,
                description=body_with_marker,
                source=SourceKind.DATA_DIR_AUDIT,
                origin_session=session_id or None,
            )
            created.append({"id": ticket.id, "title": ticket.title})
            in_flight.add(gap_id)
            log.info(
                "data_dir_audit: created draft %s — %s",
                ticket.id,
                title,
            )
        except Exception:
            log.exception(
                "data_dir_audit: failed to create draft ticket: %s",
                title,
            )
    return created


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _scan_orphan_workspaces(
    settings: Settings,
) -> tuple[dict[str, list[OrphanWorkspace]], int]:
    """Scan every board with a ``mill.db`` for orphan workspaces.

    Returns ``(orphans_by_board, total_orphans)``. Per-board failures
    are logged and skipped (the pass continues across boards).
    """
    orphans_by_board: dict[str, list[OrphanWorkspace]] = {}
    total_orphans = 0
    for board_id in _boards_from_disk(settings):
        try:
            found = find_orphan_workspaces(settings, board_id)
        except Exception:
            log.warning(
                "data_dir_audit: board=%r — orphan workspace scan failed",
                board_id,
                exc_info=True,
            )
            continue
        if not found:
            continue
        orphans_by_board[board_id] = found
        total_orphans += len(found)
        for o in found:
            log.info(
                "data_dir_audit: orphan workspace board=%r ticket=%s path=%s size=%dB",
                board_id,
                o.ticket_id,
                o.path,
                o.dir_size_bytes,
            )
    return orphans_by_board, total_orphans


def _scan_growth_deltas(settings: Settings) -> tuple[list[dict], int]:
    """Scan every board for size deltas against persisted prior-pass state.

    Returns ``(all_growth_flags, boards_with_flags)``. Current scan
    is persisted per-board, naturally pruning deleted paths.
    """
    all_growth_flags: list[dict] = []
    boards_with_flags = 0
    for board_id in _enumerate_boards(settings):
        state_path = _growth_state_path(settings, board_id)
        prior = _load_growth_state(state_path)
        board_dir = settings.data_dir / board_id
        current = _scan_board_sizes(board_dir)
        board_flags = _compute_growth_deltas(
            prior, current, settings, board_id=board_id
        )

        # Persist current scan as new state (prunes deleted paths
        # naturally — only currently-existing paths are written).
        _save_growth_state(state_path, current)

        if board_flags:
            boards_with_flags += 1
            all_growth_flags.extend(board_flags)
    return all_growth_flags, boards_with_flags


def _build_summary(
    total_bytes: int,
    total_files: int,
    oversized: list[dict],
    all_growth_flags: list[dict],
    findings: list[dict],
    orphans_by_board: dict[str, list[OrphanWorkspace]],
    total_orphans: int,
    drafts_created: list[dict],
    closed_pruned: int = 0,
) -> str:
    """Render a multi-line summary for the runs panel.

    Header is always ``"Scanned <bytes> in N files."``. When every
    check produced zero results, returns the single-line short-circuit
    ``"Scanned <bytes> in N files. No issues found."``. Otherwise each
    non-empty category contributes one line (oversized → growth →
    unbounded), the orphan line is always appended, and a final
    ``"Filed N draft(s)."`` line is added when drafts were created.
    """
    header = f"Scanned {_human_bytes(total_bytes)} in {total_files:,} files."

    if not oversized and not all_growth_flags and not findings and total_orphans == 0:
        base = header + " No issues found."
        if closed_pruned > 0:
            return base + f"\nClosed workspaces pruned: {closed_pruned}."
        return base

    lines: list[str] = [header]

    if oversized:
        n = len(oversized)
        largest = oversized[0]
        path = _trim_path(f".data/{largest['path']}")
        size = int(largest["size_bytes"])
        word = "item" if n == 1 else "items"
        lines.append(f"{n} oversized {word} (largest: {path} → {_human_bytes(size)})")

    if all_growth_flags:
        n = len(all_growth_flags)
        largest = max(all_growth_flags, key=lambda f: int(f.get("delta_bytes", 0)))
        path = _trim_path(f".data/{largest.get('board_id', '')}/{largest['path']}")
        delta = int(largest["delta_bytes"])
        word = "flag" if n == 1 else "flags"
        lines.append(f"{n} growth {word} ({path} grew by {_human_bytes(delta)})")

    if findings:
        n = len(findings)
        largest = max(findings, key=lambda f: int(f.get("current_size", 0)))
        path = _trim_path(f".data/{largest['path']}")
        current = int(largest["current_size"])
        cap = int(largest["cap_size"])
        word = "candidate" if n == 1 else "candidates"
        lines.append(
            f"{n} unbounded {word} "
            f"({path}: {_human_bytes(current)}, cap: {_human_bytes(cap)})"
        )

    word = "workspace" if total_orphans == 1 else "workspaces"
    orphan_line = f"{total_orphans} orphan {word}"
    if total_orphans > 0 and orphans_by_board:
        flat = [o for items in orphans_by_board.values() for o in items]
        if flat:
            biggest = max(flat, key=lambda o: o.dir_size_bytes)
            path = _trim_path(
                f".data/{biggest.board_id}/workspaces/{biggest.ticket_id}"
            )
            orphan_line += f" (largest: {path}, {_human_bytes(biggest.dir_size_bytes)})"
    lines.append(orphan_line)

    if drafts_created:
        n = len(drafts_created)
        word = "draft" if n == 1 else "drafts"
        lines.append(f"Filed {n} {word}.")

    if closed_pruned > 0:
        lines.append(f"Closed workspaces pruned: {closed_pruned}.")

    return "\n".join(lines)


def run_data_dir_audit_pass(
    session_id: str = "",
    repo_config: RepoConfig | None = None,
) -> DataDirAuditPassResult:
    """Execute one data-dir audit pass.

    Runs the top-N largest-items check (ticket 2), the
    unbounded-collection candidate check (ticket 4) over
    ``settings.data_dir``, the orphan-workspace check (ticket 5) over
    every board with a ``mill.db`` on disk, and the growth-delta check
    (ticket 3) which scans each board for size deltas against
    persisted prior-pass state. Inspection logic for the remaining
    checks (filing & dedup, rich summary) is added by child tickets
    6 and 7.

    Args:
        session_id: Langfuse session id from the poll loop (optional).
        repo_config: Per-repo config (optional — unused in this pass).

    Returns:
        ``DataDirAuditPassResult`` whose ``oversized_items`` list
        contains items above the configured size threshold, whose
        ``findings`` list contains flagged unbounded-collection
        candidates, whose ``growth_flags`` list contains the
        growth-delta flags from all scanned boards, and whose
        ``summary`` reflects the per-check counts.
    """
    # Settings is instantiated here so that any environment-variable
    # parsing errors surface early, and so tests can monkeypatch
    # ``robotsix_mill.data_dir_audit_runner.Settings`` to inject a
    # tmp_path-rooted instance.
    settings = Settings()

    # Opt-in GC (this ticket): prune workspace dirs of terminal-state
    # tickets BEFORE size measurement, so every downstream measurement
    # (oversized / growth / orphan) and therefore every filed alert
    # reflects the post-GC state. Default-off via the knob.
    closed_pruned = 0
    if settings.data_dir_audit_prune_closed:
        closed_pruned = _prune_closed_workspaces(settings)

    # Walk ``data_dir`` exactly once: the size dicts feed both the
    # top-N oversized check (ticket 2) and the summary header's
    # total-bytes / total-files anchor.
    if settings.data_dir.is_dir():
        file_sizes, dir_totals = _collect_sizes(settings.data_dir)
    else:
        file_sizes, dir_totals = {}, defaultdict(int)
    total_bytes = sum(file_sizes.values())
    total_files = len(file_sizes)
    oversized = _select_largest_from_sizes(
        file_sizes,
        dir_totals,
        10,
        settings.data_dir_audit_size_threshold_bytes,
    )

    # Unbounded-collection candidate detection (ticket 4 of the epic).
    findings = check_unbounded_candidates(settings.data_dir, settings)

    # Orphan-workspace detection (ticket 5 of the epic). Ticket filing
    # is intentionally out of scope (ticket 6 consumes these findings
    # via the memory-ledger dedup path).
    orphans_by_board, total_orphans = _scan_orphan_workspaces(settings)

    # Growth-delta detection (ticket 3 of the epic).
    all_growth_flags, _boards_with_flags = _scan_growth_deltas(settings)

    # ----- Filing logic (ticket 6 of the epic) -----
    # Resolve a TicketService against the scheduling board. With no
    # repo_config or an empty board_id there is no board to file
    # against — skip filing entirely (still return the inspection
    # results so the runs panel can show what was found).
    drafts_created: list[dict] = []
    if repo_config is not None and repo_config.board_id:
        service = TicketService(settings, board_id=repo_config.board_id)
        drafts_created = _file_findings_as_tickets(
            settings,
            service,
            oversized,
            all_growth_flags,
            findings,
            orphans_by_board,
            session_id=session_id,
        )

    # ----- Summary covering all checks (ticket 7 of the epic) -----
    summary = _build_summary(
        total_bytes,
        total_files,
        oversized,
        all_growth_flags,
        findings,
        orphans_by_board,
        total_orphans,
        drafts_created,
        closed_pruned=closed_pruned,
    )

    log.info("data-dir audit pass done: %s", summary)

    return DataDirAuditPassResult(
        drafts_created=drafts_created,
        oversized_items=oversized,
        summary=summary,
        updated_memory="",
        session_id=session_id,
        findings=findings,
        growth_flags=all_growth_flags,
        closed_pruned=closed_pruned,
    )
