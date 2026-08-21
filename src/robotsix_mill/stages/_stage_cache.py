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
    """Return the cached :class:`Outcome` when *input_hash* matches, else ``None``.

    A cached BLOCKED outcome is never served — see :func:`_update`. The check
    is repeated here, not just at write time, so workspaces that already hold
    a poisoned entry recover on the next pass instead of needing the file
    deleted by hand.
    """
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
    if next_state is State.BLOCKED:
        log.info(
            "%s: ignoring cached BLOCKED outcome (hash=%s…) — re-running the stage",
            stage_name,
            input_hash[:12],
        )
        return None
    note = entry.get("note", "")
    return Outcome(next_state=next_state, note=note)


def _update(ws: Workspace, stage_name: str, input_hash: str, outcome: Outcome) -> None:
    """Persist *outcome* keyed by *stage_name* and *input_hash*.

    BLOCKED outcomes are deliberately NOT cached. The cache's premise is
    "same input, so the same result — skip the expensive re-run", and that
    premise does not hold for BLOCKED: it means a human must intervene, and
    every way of intervening (resume-blocked, a code fix, a config change)
    exists precisely to produce a *different* result on the next pass.

    Caching it made a blocked ticket unrecoverable unless its description
    changed. Observed 2026-07-31: ticket …-22ec was resumed after the refine
    fix that specifically addressed it had been deployed, and the stage
    logged ``refine cache hit (hash=6dce913eed7a…) → blocked`` — replaying
    the pre-fix outcome verbatim, note and all, without running the fixed
    code at all. Three separate root-cause fixes could not reach the tickets
    they were written for.

    Not caching costs one re-run per deliberate resume, which is exactly when
    the re-run is wanted. Blocked tickets have no automated stage, so the
    reconcile sweep does not re-enqueue them in a loop.
    """
    from ..core.states import State

    if outcome.next_state is State.BLOCKED:
        return
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


_REFINE_MODULE_HASH: str | None = None


def _compute_refine_module_hash() -> str:
    """Return a stable SHA-256 hash over the refine pipeline's Python sources.

    When the refine module files change (e.g. a gate fix lands), the
    hash changes, forcing the refine stage-cache to miss and produce a
    fresh outcome rather than replaying a pre-fix verdict.  The hash is
    computed once and cached on the module — the refine sources do not
    change during the mill's lifetime.
    """
    refine_dir = Path(__file__).resolve().parent / "refine"
    if not refine_dir.is_dir():
        return "no-refine-dir"
    h = hashlib.sha256()
    for py_file in sorted(refine_dir.glob("**/*.py")):
        try:
            h.update(py_file.read_bytes())
        except OSError:
            log.debug(
                "Failed to read %s for refine-module hash", py_file, exc_info=True
            )
    return h.hexdigest()


def refine_input_hash(ws: Workspace) -> str:
    """Compute the input hash for the refine stage.

    Combines the current ticket description content (the primary input
    that determines the refine agent's output) with a hash of the
    refine pipeline's own source code, so a pipeline-code change (e.g.
    a gate fix) automatically invalidates the stage cache and forces a
    fresh re-refine.

    This reads the workspace file directly so it reflects whatever is
    on disk at call time.

    IMPORTANT: refine rewrites ``description.md`` (draft → spec), so
    the description-hash component changes between the first successful
    refine and any subsequent re-entry.  The cache therefore only hits
    on runs where both the on-disk content AND the refine module code
    are unchanged from the previous run — which is exactly the
    tail-collapse scenario (repeated polls over the same already-refined
    spec with the same pipeline code).
    """
    global _REFINE_MODULE_HASH
    if _REFINE_MODULE_HASH is None:
        _REFINE_MODULE_HASH = _compute_refine_module_hash()

    h = hashlib.sha256()
    h.update(ws.content_hash().encode("utf-8", errors="replace"))
    h.update(_REFINE_MODULE_HASH.encode("utf-8", errors="replace"))
    return h.hexdigest()


def reviewer_fingerprint(repo_dir: Path | None = None) -> str:
    """Identify the reviewer that would run, not just what it would read.

    Everything the review agent is *told* — its system prompt and any
    repo-specific conventions injected alongside the diff — is an input to
    its verdict just as much as the diff is. Leaving it out of the cache key
    means a fix to mill's own reviewer is invisible to every workspace
    holding a cached outcome: the stale verdict replays forever and the
    ticket keeps failing for a reason that has already been fixed.

    Live, that deadlocked central-deploy de52. Its reviewer kept asking for a
    changelog fragment that implement deletes by design on a release-please
    repo. The reviewer-side fix shipped in a34839e3 and deployed, but the
    workspace replayed a 04:06 REQUEST_CHANGES on every pass — the diff and
    HEAD had not moved, so the cache hit — and the ticket burned its
    implement/review ceiling a second time without the new reviewer ever
    running once.

    Returns "" on any failure: a fingerprint we cannot compute must not stop
    the review from being cached at all.
    """
    try:
        from ..agents.reviewing import SYSTEM_PROMPT, _repo_conventions

        h = hashlib.sha256()
        h.update(SYSTEM_PROMPT.encode("utf-8", errors="replace"))
        h.update(_repo_conventions(repo_dir).encode("utf-8", errors="replace"))
        return h.hexdigest()
    except Exception:
        log.debug("Failed to fingerprint the reviewer", exc_info=True)
        return ""


def review_input_hash(
    ws: Workspace, diff: str, head_sha: str = "", repo_dir: Path | None = None
) -> str:
    """Compute the input hash for the review stage.

    Based on the ticket description (the spec), the implementation
    diff, the branch-tip HEAD SHA, and a fingerprint of the reviewer
    itself.  Including *head_sha* ensures that after a rebase or
    force-push (new HEAD SHA) the cache misses even when the diff
    text is unchanged, forcing a fresh review against the current
    branch tip.  Including the reviewer fingerprint does the same when
    mill's own review prompt or a repo's conventions change — see
    :func:`reviewer_fingerprint` for why that matters.
    """
    h = hashlib.sha256()
    h.update(ws.read_description().encode("utf-8", errors="replace"))
    h.update(diff.encode("utf-8", errors="replace"))
    h.update(head_sha.encode("utf-8", errors="replace"))
    h.update(reviewer_fingerprint(repo_dir).encode("utf-8", errors="replace"))
    return h.hexdigest()
