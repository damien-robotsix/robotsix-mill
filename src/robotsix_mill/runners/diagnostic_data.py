"""Shared diagnostic data-access layer for the daily diagnostic agent.

This module sits alongside ``diagnostic_runner`` / ``diagnostic_checks``
and provides the normalized, **fail-safe** data-access utilities that
every diagnostic check consumes. It spans the two diagnostic data
sources:

1. **Runs logs** — per-board ``<data_dir>/<board_id>/runs.json``, modeled
   by :class:`~robotsix_mill.runtime.run_registry.RunRegistry`. The
   registry exposes only ``most_recent`` / ``list_all``; this layer adds
   the time/status filtering the error-detection check needs.
2. **Langfuse** — thin ``repo_id`` → :class:`RepoConfig`-credential
   resolving wrappers over the existing
   :mod:`robotsix_mill.langfuse.client` read helpers (this module never
   re-implements HTTP — it only resolves credentials, delegates, and
   normalizes).

Every public function is fail-safe: it catches its own failures, logs
them via the module-level ``log``, and returns an empty/``None`` result
rather than raising. A data-source outage must never crash a diagnostic
check or the pass.
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import RepoConfig, Settings, load_repos_config
from ..langfuse import client as langfuse_client
from ..runtime.run_registry import RunRegistry

log = logging.getLogger(__name__)


# -- runs-logs utilities ---------------------------------------------------


def query_runs(
    board_id: str,
    *,
    kind: str | None = None,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """Return run entries for *board_id*, optionally filtered.

    Opens the board's registry directly
    (``<data_dir>/<board_id>/runs.json``) and starts from
    ``registry.list_all()`` (newest first; each entry is the
    ``RunEntry`` dict shape: ``id, kind, started_at, finished_at,
    status, summary, error, repo_id``). A board with no ``runs.json``
    yields ``[]`` (``RunRegistry.__init__`` tolerates a missing/corrupt
    file).

    Args:
        board_id: Board whose ``runs.json`` to read.
        kind: When set, keep only entries whose ``kind`` matches exactly.
        status: When set, keep only entries whose ``status`` matches
            exactly (e.g. ``"error"``, ``"ok"``, ``"running"``).
        since: Inclusive ISO-8601 UTC lower bound on ``started_at``.
        until: Exclusive ISO-8601 UTC upper bound on ``started_at``.
        settings: Settings to use; when ``None`` a parameterless
            ``Settings()`` is constructed (matches the convention used by
            ``run_diagnostic_pass`` and the existing runners).

    All filters are ANDed together. The ``since``/``until`` bounds are
    compared against ``entry["started_at"]`` using plain string
    (lexicographic) comparison — this is correct for ISO-8601 UTC
    timestamps and is the simplest approach; entries with a
    malformed/missing ``started_at`` are skipped from time-bounded
    results rather than raising.

    Returns the filtered list of dicts in newest-first order. On any
    unexpected error, logs and returns ``[]``.
    """
    try:
        if settings is None:
            settings = Settings()
        registry = RunRegistry(settings.data_dir / board_id / "runs.json")
        entries = registry.list_all()

        result: list[dict[str, Any]] = []
        for entry in entries:
            if kind is not None and entry.get("kind") != kind:
                continue
            if status is not None and entry.get("status") != status:
                continue
            if since is not None or until is not None:
                started_at = entry.get("started_at")
                if not isinstance(started_at, str):
                    # Defensive: a malformed/missing timestamp can't be
                    # placed in a time window — skip it.
                    continue
                if since is not None and started_at < since:
                    continue
                if until is not None and started_at >= until:
                    continue
            result.append(entry)
        return result
    except Exception:  # noqa: BLE001 — a data-source outage must not crash callers
        log.exception("query_runs failed for board %s", board_id)
        return []


def query_run_errors(
    board_id: str,
    *,
    since: str | None = None,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """Return the ``status == "error"`` run entries for *board_id*.

    Thin convenience wrapper over :func:`query_runs` with
    ``status="error"`` — the shape the error-detection check uses.
    Optionally time-bounded with an inclusive ISO-8601 UTC *since*.
    Fail-safe ``[]`` on any error (inherited from :func:`query_runs`).
    """
    return query_runs(board_id, status="error", since=since, settings=settings)


# -- Langfuse utilities ----------------------------------------------------


def _repo_config_for(
    repo_id: str, *, settings: Settings | None = None
) -> RepoConfig | None:
    """Resolve *repo_id* to its :class:`RepoConfig`, or ``None``.

    Looks the repo up through :func:`load_repos_config`. The synthetic
    cross-repo *meta* board is honored by returning the registry's
    ``meta`` config when *repo_id* matches the meta board id. When the
    repo is unknown, logs a warning and returns ``None`` (the downstream
    client helpers treat ``repo_config=None`` as "use global secrets",
    but for diagnostics an unknown ``repo_id`` is a configuration error
    worth logging).

    Fail-safe: returns ``None`` on any unexpected error.
    """
    try:
        from ..runtime.worker.core import Worker

        registry = load_repos_config()
        if repo_id == Worker._META_BOARD:
            return registry.meta
        repo_config = registry.repos.get(repo_id)
        if repo_config is None:
            log.warning("diagnostic data: unknown repo_id %r", repo_id)
        return repo_config
    except Exception:  # noqa: BLE001 — never crash the caller
        log.exception("_repo_config_for failed for repo %s", repo_id)
        return None


def _normalize_trace(raw: dict[str, Any]) -> dict[str, Any]:
    """Project a raw Langfuse trace dict into a stable, documented shape.

    Downstream checks depend on this normalized shape rather than
    Langfuse's raw field names. Every field uses ``.get`` so a missing
    key yields ``None`` (never a ``KeyError``):

    - ``trace_id`` ← ``raw["id"]``
    - ``name`` ← ``raw["name"]``
    - ``session_id`` ← ``raw["sessionId"]``
    - ``total_cost`` ← ``raw["totalCost"]``
    - ``timestamp`` ← ``raw["timestamp"]``
    - ``observation_summary`` — compact per-trace summary (model,
      token counts, tool-call list, error/warning counts) sourced
      from :func:`robotsix_mill.langfuse.client.trace_observation_summary`
    """
    from ..langfuse.client import trace_observation_summary

    return {
        "trace_id": raw.get("id"),
        "name": raw.get("name"),
        "session_id": raw.get("sessionId"),
        "total_cost": raw.get("totalCost"),
        "timestamp": raw.get("timestamp"),
        "observation_summary": trace_observation_summary(raw),
    }


def query_traces_since(
    repo_id: str, from_timestamp: str, *, settings: Settings | None = None
) -> list[dict[str, Any]]:
    """Return *repo_id*'s Langfuse traces at/after *from_timestamp*.

    Resolves the repo's credentials via :class:`RepoConfig` and delegates
    to :func:`robotsix_mill.langfuse.client.list_all_traces_since` (which
    already returns ``[]`` and never raises when Langfuse is
    unconfigured/unreachable). Each raw trace is mapped through
    :func:`_normalize_trace`.

    Args:
        repo_id: Repo whose Langfuse credentials to use.
        from_timestamp: ISO-8601 lower bound passed to the client helper.
        settings: Settings to use; ``None`` → parameterless ``Settings()``.

    Returns a list of normalized trace dicts. On any unexpected error,
    logs and returns ``[]``.
    """
    try:
        if settings is None:
            settings = Settings()
        repo_config = _repo_config_for(repo_id, settings=settings)
        raw_traces = langfuse_client.list_all_traces_since(
            settings, from_timestamp, repo_config
        )
        return [_normalize_trace(t) for t in raw_traces]
    except Exception:  # noqa: BLE001 — Langfuse outage must not crash callers
        log.exception("query_traces_since failed for repo %s", repo_id)
        return []


def query_recent_traces(
    repo_id: str, *, limit: int = 10, settings: Settings | None = None
) -> list[dict[str, Any]]:
    """Return up to *limit* of *repo_id*'s most-recent Langfuse traces.

    Resolves the repo's credentials via :class:`RepoConfig` and delegates
    to :func:`robotsix_mill.langfuse.client.list_recent_traces` (which
    returns ``[]`` and never raises when Langfuse is unconfigured). Each
    raw trace is mapped through :func:`_normalize_trace`.

    Returns a list of normalized trace dicts. On any unexpected error,
    logs and returns ``[]``.
    """
    try:
        if settings is None:
            settings = Settings()
        repo_config = _repo_config_for(repo_id, settings=settings)
        raw_traces = langfuse_client.list_recent_traces(
            settings, limit=limit, repo_config=repo_config
        )
        return [_normalize_trace(t) for t in raw_traces]
    except Exception:  # noqa: BLE001 — Langfuse outage must not crash callers
        log.exception("query_recent_traces failed for repo %s", repo_id)
        return []


def query_session_summary(
    repo_id: str, session_id: str, *, settings: Settings | None = None
) -> str | None:
    """Return a text summary of *session_id*'s traces, or ``None``.

    Resolves the repo's credentials via :class:`RepoConfig` and delegates
    to :func:`robotsix_mill.langfuse.client.fetch_session_summary` (which
    already returns ``None`` when Langfuse is unconfigured/unreachable).

    Returns the summary string, or ``None`` when unavailable. On any
    unexpected error, logs and returns ``None``.
    """
    try:
        if settings is None:
            settings = Settings()
        repo_config = _repo_config_for(repo_id, settings=settings)
        return langfuse_client.fetch_session_summary(settings, session_id, repo_config)
    except Exception:  # noqa: BLE001 — Langfuse outage must not crash callers
        log.exception("query_session_summary failed for repo %s", repo_id)
        return None
