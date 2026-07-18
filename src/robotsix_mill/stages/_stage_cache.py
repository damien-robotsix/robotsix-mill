"""Per-stage outcome cache keyed on input hash.

When a stage's input (ticket description, diff) is unchanged from the
last successful run, the cached outcome is returned immediately —
short-circuiting repeated re-check / re-refine passes that would
otherwise produce identical results and burn subscription headroom.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from ..core.workspace import Workspace
from .base import Outcome

log = logging.getLogger("robotsix_mill.stages.cache")

_CACHE_FILENAME = "stage_cache.json"


def _cache_path(ws: Workspace) -> Path:
    return ws.artifacts_dir / _CACHE_FILENAME


def _load(ws: Workspace) -> dict[str, Any]:
    p = _cache_path(ws)
    if not p.exists():
        return {}
    try:
        data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
        return data
    except Exception:
        log.debug("Failed to load stage cache, starting fresh", exc_info=True)
        return {}


def _save(ws: Workspace, data: dict[str, Any]) -> None:
    p = _cache_path(ws)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    except OSError:
        log.debug("Failed to persist stage cache", exc_info=True)


def _check(ws: Workspace, stage_name: str, input_hash: str) -> Outcome | None:
    """Return the cached :class:`Outcome` when *input_hash* matches, else ``None``."""
    cache = _load(ws)
    entry = cache.get(stage_name)
    if entry is None:
        return None
    if entry.get("input_hash") != input_hash:
        return None
    from ..core.states import State

    state_raw = entry.get("next_state")
    if state_raw is None:
        return None
    try:
        next_state = State(state_raw)
    except ValueError:
        return None
    note = entry.get("note", "")
    return Outcome(next_state=next_state, note=note)


def _update(ws: Workspace, stage_name: str, input_hash: str, outcome: Outcome) -> None:
    """Persist *outcome* keyed by *stage_name* and *input_hash*."""
    cache = _load(ws)
    cache[stage_name] = {
        "input_hash": input_hash,
        "next_state": outcome.next_state.value,
        "note": outcome.note or "",
    }
    _save(ws, cache)


def _invalidate(ws: Workspace, stage_name: str) -> None:
    """Remove the cached entry for *stage_name*, if present."""
    cache = _load(ws)
    cache.pop(stage_name, None)
    _save(ws, cache)


def refine_input_hash(ws: Workspace) -> str:
    """Compute the input hash for the refine stage.

    Based on the current ticket description content — the primary input
    that determines the refine agent's output.  This reads the workspace
    file directly so it reflects whatever is on disk at call time.

    IMPORTANT: refine rewrites ``description.md`` (draft → spec), so
    the hash changes between the first successful refine and any
    subsequent re-entry.  The cache therefore only hits on runs where
    the on-disk content is unchanged from the previous run — which is
    exactly the tail-collapse scenario (repeated polls over the same
    already-refined spec).
    """
    return ws.content_hash()


def review_input_hash(ws: Workspace, diff: str, head_sha: str = "") -> str:
    """Compute the input hash for the review stage.

    Based on the ticket description (the spec), the implementation
    diff, and the branch-tip HEAD SHA — the three inputs the review
    agent sees.  Including *head_sha* ensures that after a rebase or
    force-push (new HEAD SHA) the cache misses even when the diff
    text is unchanged, forcing a fresh review against the current
    branch tip.
    """
    h = hashlib.sha256()
    h.update(ws.read_description().encode("utf-8", errors="replace"))
    h.update(diff.encode("utf-8", errors="replace"))
    h.update(head_sha.encode("utf-8", errors="replace"))
    return h.hexdigest()
