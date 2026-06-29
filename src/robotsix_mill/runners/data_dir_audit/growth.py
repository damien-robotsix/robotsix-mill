"""Growth-delta tracking with persistent prior-pass state (ticket 3).

Includes board enumeration, size-scan, growth-delta computation,
and growth classification with breakdown.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any
from pathlib import Path

from sqlmodel import select

from ...config import Settings
from ...core import db
from ...core.models import Ticket
from ...core.states import State

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


# ---------------------------------------------------------------------------
# Path classification helpers
# ---------------------------------------------------------------------------


def _workspace_ticket_id_for_path(path: str) -> str | None:
    """Map a growth-flag *path* to its per-ticket workspace id, if any.

    Growth-flag ``path`` values are POSIX paths relative to the board
    root (e.g. ``"workspaces/<ticket_id>/repo/.git/objects/"``,
    ``"workspaces/<ticket_id>"``, or ``"big.log"``). Returns the ticket
    id when the path lies inside ``workspaces/<ticket_id>/`` and the
    second segment looks like a ticket id; otherwise ``None``.
    """
    parts = path.split("/")
    if (
        len(parts) >= 2
        and parts[0] == "workspaces"
        and _TICKET_ID_PREFIX_RE.match(parts[1])
    ):
        return parts[1]
    return None


def _periodic_pass_workspace_subdirs() -> set[str]:
    """Return the canonical set of periodic-pass workspace subdir names.

    Derived from the single source of truth ``PERIODIC_PASS_CONFIGS`` in
    :mod:`robotsix_mill.runners.periodic_runner`, so every current and
    future periodic pass is covered without a hardcoded list. Uses a
    lazy import (mirroring the ``_verify_prior_proposals`` seam in
    :func:`_file_findings_as_tickets`) to avoid import-ordering coupling.
    """
    from ..periodic_runner import PERIODIC_PASS_CONFIGS

    return {cfg.workspace_subdir for cfg in PERIODIC_PASS_CONFIGS.values()} | {
        "periodic_workspace"
    }


def _is_periodic_pass_workspace_path(path: str) -> bool:
    """Return ``True`` when *path*'s leading segment is a periodic-pass
    workspace subdir.

    Growth-flag ``path`` values are POSIX paths relative to the scanned
    board root (e.g. ``"health_workspace/repo/.git/objects/"``), so the
    periodic-pass clone subdir is always the first segment. Matching the
    leading segment alone is sufficient — the repo_id-vs-board_id
    distinction is irrelevant. Returns ``False`` for ``"big.log"`` and
    for per-ticket ``"workspaces/<ticket_id>/..."`` paths.
    """
    first_segment = path.split("/", 1)[0]
    return first_segment in _periodic_pass_workspace_subdirs()


# ---------------------------------------------------------------------------
# State file helpers (ticket 3: growth-delta tracking)
# ---------------------------------------------------------------------------


def _growth_state_path(settings: Settings, board_id: str) -> Path:
    """Return the per-board persistent state file path.

    Matches the board-scoped pattern used by
    :func:`trace_review_runner._state_path`.
    """
    return settings.data_dir / board_id / "data_dir_audit_state.json"


def _load_growth_state(state_path: Path) -> dict[str, dict[str, Any]]:
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


def _save_growth_state(state_path: Path, state: dict[str, dict[str, Any]]) -> None:
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


# SQLite transient sidecar suffixes excluded from growth tracking (they
# fluctuate with checkpoints and are not real disk growth).
_SQLITE_TRANSIENT_SUFFIXES = (".db-wal", ".db-shm", ".db-journal")


def _record_entry(
    entry: Path, board_dir: Path, result: dict[str, dict[str, Any]]
) -> None:
    """Add a single filesystem *entry* to *result* if it qualifies.

    Symlinks, unreadable entries, the audit state file, and SQLite's
    transient sidecar files are skipped silently.
    """
    if entry.is_symlink():
        return
    # SQLite's transient sidecars (WAL / shared-memory / rollback journal)
    # fluctuate normally with checkpoints — a growing ``.db-wal`` is buffered
    # writes pending a checkpoint, not a disk leak. Tracking them produces
    # recurring false-positive "growth mill.db-wal" findings, so skip them
    # (the backing ``.db`` is still tracked).
    if entry.name.endswith(_SQLITE_TRANSIENT_SUFFIXES):
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


def _compute_cumulative_dir_sizes(result: dict[str, dict[str, Any]]) -> None:
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


def _scan_board_sizes(board_dir: Path) -> dict[str, dict[str, Any]]:
    """Walk *board_dir* and record file sizes + cumulative directory sizes.

    Returns a dict mapping POSIX relative paths to
    ``{"size_bytes": int, "mtime": float}``.

    - Files: ``st_size`` + ``st_mtime``.
    - Directories: cumulative size of all files under them (recursive)
      + the directory's own ``st_mtime``.
    - Symlinks are skipped entirely (not followed, not measured).
    - The state file itself (``data_dir_audit_state.json``) is excluded.
    """
    result: dict[str, dict[str, Any]] = {}
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
    prior: dict[str, dict[str, Any]],
    current: dict[str, dict[str, Any]],
    settings: Settings,
    board_id: str = "",
) -> list[dict[str, Any]]:
    """Compare *prior* and *current* size snapshots; flag excessive growth.

    For every path present in *both* snapshots:
    - Compute ``delta_bytes = current_size - prior_size``.
    - Skip if ``delta_bytes <= 0`` (shrank or unchanged).
    - Compute ``delta_pct``; guard against division by zero.
    - Flag if ``delta_bytes >= growth_delta_bytes`` **OR**
      (``delta_pct >= growth_delta_pct`` **AND**
      ``delta_bytes >= growth_delta_pct_min_bytes``).

    The percentage check is gated behind a minimum absolute delta
    (``growth_delta_pct_min_bytes``, default 1 MiB) so that tiny
    baselines (e.g. small JSON state files that swing +100% on a
    single-entry insert while growing only a few KB) do not produce
    false-positive growth flags.

    Returns a list of flag-dicts (empty if nothing flagged).
    """
    flags: list[dict[str, Any]] = []
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
        min_abs = settings.data_dir_audit_growth_delta_pct_min_bytes
        if delta_pct >= threshold_pct and delta_bytes >= min_abs:
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
# Growth classification + breakdown
# ---------------------------------------------------------------------------

# Classifications for growth-flag paths and their contributors. A path
# is "self-healing" when the system already reclaims or reports it
# through another channel, so a growth ticket would be pure noise.
_GROWTH_CLASS_ACTIVE = "active ticket workspace (transient)"
_GROWTH_CLASS_TERMINAL = "terminal ticket workspace (clone GC reclaims)"
_GROWTH_CLASS_ORPHAN = "orphan workspace (reported by the orphan check)"
_GROWTH_CLASS_PERIODIC = "periodic-pass clone (re-cloned every pass)"
_GROWTH_CLASS_META_CLONE_CACHE = "meta board clone cache (transient, re-cloned)"
_GROWTH_CLASS_MEMORY_LEDGER = "bounded memory ledger (capped by max_memory_chars)"
_GROWTH_CLASS_RUN_REGISTRY = "bounded run registry (capped at 50 entries)"
_GROWTH_CLASS_CANDIDATES = (
    "bounded candidates ledger (capped by retrospect_candidates_max_entries)"
)
_GROWTH_CLASS_DB = "bounded board database (per-ticket/archive retention caps)"
_GROWTH_CLASS_OTHER = "other"

# Fraction of a directory's growth that must be attributable to
# self-healing categories for the aggregate flag to be suppressed —
# e.g. ``workspaces/`` growing only because active tickets grew.
_EXPLAINED_SUPPRESS_FRACTION = 0.9

# Top-N contributors listed in a filed growth ticket's breakdown.
_BREAKDOWN_TOP_N = 8


def _workspace_ticket_states(
    settings: Settings, board_id: str, ticket_ids: set[str]
) -> dict[str, str]:
    """Map each id in *ticket_ids* to ``"active"`` / ``"terminal"`` /
    ``"orphan"`` (no DB row) via batched ``IN`` selects."""
    states: dict[str, str] = {tid: "orphan" for tid in ticket_ids}
    ids = sorted(ticket_ids)
    with db.session(settings, board_id) as s:
        for start in range(0, len(ids), _BATCH_SIZE):
            chunk = ids[start : start + _BATCH_SIZE]
            rows = s.exec(
                select(Ticket.id, Ticket.state).where(Ticket.id.in_(chunk))  # type: ignore[attr-defined]
            ).all()
            for tid, state in rows:
                states[tid] = (
                    "terminal" if State(state) in _TERMINAL_STATES else "active"
                )
    return states


def _is_meta_clone_cache_path(path: str) -> bool:
    """Return ``True`` when *path* lies in the meta board's clone cache.

    The meta board persists a clone cache at the board-root ``workspace/``
    (singular) directory, which fluctuates naturally as upstream repos
    update. Growth-flag ``path`` values are POSIX paths relative to the
    board root, so the leading segment is ``workspace`` for both the
    aggregate ``workspace/`` flag and per-repo ``workspace/<repo>/...``
    flags. This is distinct from the per-ticket ``workspaces/`` (plural)
    directory matched by :func:`_workspace_ticket_id_for_path`.
    """
    return path.split("/", 1)[0] == "workspace"


def _classify_growth_path(path: str, ticket_states: dict[str, str]) -> str:
    """Classify a growth path against the workspace/periodic taxonomy."""
    if _is_meta_clone_cache_path(path):
        return _GROWTH_CLASS_META_CLONE_CACHE
    if _is_periodic_pass_workspace_path(path):
        return _GROWTH_CLASS_PERIODIC
    tid = _workspace_ticket_id_for_path(path)
    if tid is None:
        if path == "runs.json":
            return _GROWTH_CLASS_RUN_REGISTRY
        if path == "mill.db":
            return _GROWTH_CLASS_DB
        if path.endswith("_memory.md"):
            return _GROWTH_CLASS_MEMORY_LEDGER
        if path == "AGENT_CANDIDATES.md" or path.endswith("/AGENT_CANDIDATES.md"):
            return _GROWTH_CLASS_CANDIDATES
        return _GROWTH_CLASS_OTHER
    state = ticket_states.get(tid, "orphan")
    if state == "active":
        return _GROWTH_CLASS_ACTIVE
    if state == "terminal":
        return _GROWTH_CLASS_TERMINAL
    return _GROWTH_CLASS_ORPHAN


def _immediate_child_growth(
    prior: dict[str, dict[str, Any]],
    current: dict[str, dict[str, Any]],
    parent: str,
) -> list[dict[str, Any]]:
    """Positive growth deltas of *parent*'s immediate children.

    *parent* must be a directory key (trailing ``/``). A child absent
    from *prior* counts with its full current size — new data is
    growth too, even though :func:`_compute_growth_deltas` only flags
    paths present in both snapshots. Returns
    ``[{"path": ..., "delta_bytes": ...}, ...]`` sorted descending.
    """
    out: list[dict[str, Any]] = []
    for key, info in current.items():
        if key == parent or not key.startswith(parent):
            continue
        rest = key[len(parent) :].rstrip("/")
        if not rest or "/" in rest:
            continue
        prior_size = prior.get(key, {}).get("size_bytes", 0)
        delta = info["size_bytes"] - prior_size
        if delta > 0:
            out.append({"path": key, "delta_bytes": delta})
    out.sort(key=lambda d: int(d["delta_bytes"]), reverse=True)
    return out


def _annotate_and_filter_growth_flags(
    settings: Settings,
    board_id: str,
    flags: list[dict[str, Any]],
    prior: dict[str, dict[str, Any]],
    current: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Suppress self-healing growth flags; attach a breakdown to the rest.

    Suppression rules (each logged):

    - periodic-pass workspace paths — wiped and re-cloned every pass;
    - meta board clone cache paths (``workspace/...``) — transient
      infrastructure that fluctuates as upstream repos update;
    - paths inside ACTIVE ticket workspaces — expected transient
      runtime data (e.g. the ``repo/`` clone);
    - paths inside TERMINAL ticket workspaces when the terminal-clone
      GC is enabled — it reclaims them on the next pass;
    - paths inside ORPHAN workspaces — the orphan check files its own
      finding with the full directory size;
    - directory flags whose growth is ``>= _EXPLAINED_SUPPRESS_FRACTION``
      attributable to the above categories via their immediate children
      — e.g. the aggregate ``workspaces/`` dir growing only because the
      (individually suppressed) per-ticket workspaces inside it grew.

    Surviving flags gain ``breakdown`` (top contributors, classified)
    and ``explained_pct`` so the filed ticket is self-diagnosing
    without requiring data-dir access from any agent.
    """
    child_cache = {
        flag["path"]: _immediate_child_growth(prior, current, flag["path"])
        for flag in flags
        if flag["path"].endswith("/")
    }
    ticket_states = _growth_ticket_states(settings, board_id, flags, child_cache)
    prune_terminal = settings.data_dir_audit_prune_terminal_clones

    def _self_healing(cls: str) -> bool:
        if cls == _GROWTH_CLASS_TERMINAL:
            return prune_terminal
        return cls != _GROWTH_CLASS_OTHER

    kept: list[dict[str, Any]] = []
    for flag in flags:
        path = flag["path"]
        delta = int(flag["delta_bytes"])
        cls = _classify_growth_path(path, ticket_states)
        if _self_healing(cls):
            log.info(
                "data_dir_audit: suppressing growth flag board=%r path=%s "
                "delta=%dB (%s)",
                board_id,
                path,
                delta,
                cls,
            )
            continue
        children = child_cache.get(path, [])
        explained = _classify_children(children, ticket_states, _self_healing)
        explained_pct = min(100.0, (explained / delta) * 100) if delta > 0 else 0.0
        if children and explained_pct >= _EXPLAINED_SUPPRESS_FRACTION * 100:
            log.info(
                "data_dir_audit: suppressing growth flag board=%r path=%s "
                "delta=%dB (%.0f%% attributable to self-healing workspace "
                "churn)",
                board_id,
                path,
                delta,
                explained_pct,
            )
            continue
        flag["breakdown"] = children[:_BREAKDOWN_TOP_N]
        flag["explained_pct"] = round(explained_pct, 1)
        kept.append(flag)
    return kept


def _growth_ticket_states(
    settings: Settings,
    board_id: str,
    flags: list[dict[str, Any]],
    child_cache: dict[str, list[dict[str, Any]]],
) -> dict[str, str]:
    """One batched state lookup covering every ticket id seen in flag
    paths or their immediate children."""
    tids: set[str] = set()
    for flag in flags:
        tid = _workspace_ticket_id_for_path(flag["path"])
        if tid is not None:
            tids.add(tid)
    for children in child_cache.values():
        for child in children:
            ctid = _workspace_ticket_id_for_path(child["path"])
            if ctid is not None:
                tids.add(ctid)
    return _workspace_ticket_states(settings, board_id, tids) if tids else {}


def _classify_children(
    children: list[dict[str, Any]],
    ticket_states: dict[str, str],
    self_healing: Callable[[str], bool],
) -> int:
    """Stamp each child's classification in place; return the summed
    growth of the self-healing ones."""
    explained = 0
    for child in children:
        ccls = _classify_growth_path(child["path"], ticket_states)
        child["classification"] = ccls
        if self_healing(ccls):
            explained += int(child["delta_bytes"])
    return explained


# ---------------------------------------------------------------------------
# Runner helper — scans growth across all boards
# ---------------------------------------------------------------------------


def _scan_growth_deltas(settings: Settings) -> tuple[list[dict[str, Any]], int]:
    """Scan every board for size deltas against persisted prior-pass state.

    Returns ``(all_growth_flags, boards_with_flags)``. Current scan
    is persisted per-board, naturally pruning deleted paths.
    """
    all_growth_flags: list[dict[str, Any]] = []
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

        # Suppress self-healing growth flags (periodic clones, active /
        # terminal / orphan ticket workspaces, and aggregate dirs whose
        # growth those explain) and attach a classified breakdown to
        # the survivors so a filed ticket is self-diagnosing.
        if board_flags:
            board_flags = _annotate_and_filter_growth_flags(
                settings, board_id, board_flags, prior, current
            )

        if board_flags:
            boards_with_flags += 1
            all_growth_flags.extend(board_flags)
    return all_growth_flags, boards_with_flags
