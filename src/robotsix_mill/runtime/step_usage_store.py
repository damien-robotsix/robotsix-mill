"""Local SQLite mirror for per-call ``step_usage`` records.

``record_step_usage()`` stamps each record onto the active OTel span so
Langfuse can carry it, but Langfuse exposes no server-side aggregation
or field projection — reading an observation back drags its full prompt
payload along.  This module keeps a tiny local mirror (token counts and
attribution only, never prompt text) so ``GET /metrics/step-usage`` can
compute stage×model aggregates in-process without touching Langfuse at
all.

The mirror is append-only and best-effort: a failed write is logged at
DEBUG and must never affect the span attribute or the calling pipeline.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS step_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_epoch REAL NOT NULL,
    timestamp TEXT NOT NULL,
    stage_name TEXT NOT NULL DEFAULT '',
    model_name TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
    request_count INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    ticket_id TEXT NOT NULL DEFAULT '',
    board_id TEXT NOT NULL DEFAULT '',
    backend TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_step_usage_ts ON step_usage(ts_epoch);
CREATE INDEX IF NOT EXISTS idx_step_usage_stage_model
    ON step_usage(stage_name, model_name);
"""

# Retention: prune records older than this many days, opportunistically
# every ``_PRUNE_EVERY`` inserts so the append path stays cheap.
_RETENTION_DAYS = 30
_PRUNE_EVERY = 500

_lock = threading.Lock()
_conns: dict[str, sqlite3.Connection] = {}
_inserts_since_prune = 0
_default_data_dir: Path | None = None


def _resolve_default_data_dir() -> Path:
    """Resolve the process data directory once and cache it.

    ``record_step_usage`` has no ``Settings`` handle, so the mirror
    writer falls back to a lazily-constructed ``Settings()``.  The read
    path (the HTTP route) passes the app's ``settings.data_dir``
    explicitly.
    """
    global _default_data_dir
    if _default_data_dir is None:
        from ..config import Settings

        _default_data_dir = Settings().data_dir
    return _default_data_dir


def _get_conn(data_dir: Path) -> sqlite3.Connection:
    """Return a shared connection for ``<data_dir>/step_usage.db``.

    Connections are cached per path and serialized by ``_lock`` so the
    in-process worker (writer) and the HTTP route (reader) never race.
    WAL mode keeps the file safe if a second process ever reads it.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    key = str(data_dir / "step_usage.db")
    with _lock:
        conn = _conns.get(key)
        if conn is None:
            conn = sqlite3.connect(
                data_dir / "step_usage.db",
                timeout=30,
                check_same_thread=False,
            )
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
            _conns[key] = conn
        return conn


def _parse_timestamp(value: Any) -> datetime:
    """Parse an ISO-8601 timestamp, defaulting to now on any failure."""
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed
    return datetime.now(UTC)


def record(data: dict[str, Any], *, data_dir: Path | None = None) -> None:
    """Mirror one ``step_usage`` dict into the local store.

    Best-effort: any failure is logged at DEBUG and swallowed so a
    metrics-mirror problem can never break the calling pipeline.
    """
    global _inserts_since_prune
    try:
        conn = _get_conn(
            data_dir if data_dir is not None else _resolve_default_data_dir()
        )
        ts = _parse_timestamp(data.get("timestamp"))
        with _lock:
            conn.execute(
                """
                INSERT INTO step_usage (
                    ts_epoch, timestamp, stage_name, model_name,
                    input_tokens, output_tokens, cache_read_input_tokens,
                    cache_creation_input_tokens, request_count, retry_count,
                    ticket_id, board_id, backend
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts.timestamp(),
                    ts.isoformat(),
                    str(data.get("stage_name", "") or ""),
                    str(data.get("model_name", "") or ""),
                    int(data.get("input_tokens", 0) or 0),
                    int(data.get("output_tokens", 0) or 0),
                    int(data.get("cache_read_input_tokens", 0) or 0),
                    int(data.get("cache_creation_input_tokens", 0) or 0),
                    int(data.get("request_count", 0) or 0),
                    int(data.get("retry_count", 0) or 0),
                    str(data.get("ticket_id", "") or ""),
                    str(data.get("board_id", "") or ""),
                    str(data.get("backend", "") or ""),
                ),
            )
            conn.commit()
            _inserts_since_prune += 1
            if _inserts_since_prune >= _PRUNE_EVERY:
                _inserts_since_prune = 0
                cutoff = (ts - timedelta(days=_RETENTION_DAYS)).timestamp()
                conn.execute("DELETE FROM step_usage WHERE ts_epoch < ?", (cutoff,))
                conn.commit()
    except Exception:
        log.debug("step_usage mirror: record failed", exc_info=True)


def _percentile(sorted_values: Sequence[float], p: float) -> float:
    """Return the *p*-th percentile (0–100) via linear interpolation."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (p / 100.0) * (len(sorted_values) - 1)
    lo = int(rank)
    frac = rank - lo
    hi = lo + 1
    if hi >= len(sorted_values):
        return float(sorted_values[-1])
    return float(sorted_values[lo]) * (1.0 - frac) + float(sorted_values[hi]) * frac


def _summarize(values: list[int]) -> dict[str, float]:
    """Return count/sum/mean/p50/p95/max for a list of token counts."""
    if not values:
        return {"sum": 0.0, "mean": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}
    ordered = sorted(values)
    total = sum(ordered)
    return {
        "sum": float(total),
        "mean": total / len(ordered),
        "p50": _percentile(ordered, 50.0),
        "p95": _percentile(ordered, 95.0),
        "max": float(ordered[-1]),
    }


def aggregate(
    *,
    data_dir: Path,
    since: datetime,
    until: datetime,
    stage: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Aggregate mirrored records in ``[since, until)`` per stage×model.

    Returns a compact dict with one entry per (stage_name, model_name)
    pair: call count plus sum/mean/p50/p95/max for input and output
    tokens, and the cache-read share of input tokens.  No prompt text is
    stored or returned.
    """
    conn = _get_conn(data_dir)
    sql = (
        "SELECT stage_name, model_name, input_tokens, output_tokens, "
        "cache_read_input_tokens, cache_creation_input_tokens "
        "FROM step_usage WHERE ts_epoch >= ? AND ts_epoch < ?"
    )
    params: list[Any] = [since.timestamp(), until.timestamp()]
    if stage is not None:
        sql += " AND stage_name = ?"
        params.append(stage)
    if model is not None:
        sql += " AND model_name = ?"
        params.append(model)

    with _lock:
        rows = list(conn.execute(sql, params))

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for (
        stage_name,
        model_name,
        input_tokens,
        output_tokens,
        cache_read,
        cache_creation,
    ) in rows:
        key = (stage_name, model_name)
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "stage_name": stage_name,
                "model_name": model_name,
                "input_tokens": [],
                "output_tokens": [],
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            }
        group["input_tokens"].append(int(input_tokens))
        group["output_tokens"].append(int(output_tokens))
        group["cache_read_input_tokens"] += int(cache_read)
        group["cache_creation_input_tokens"] += int(cache_creation)

    result_groups: list[dict[str, Any]] = []
    for group in groups.values():
        input_summary = _summarize(group["input_tokens"])
        input_sum = input_summary["sum"]
        result_groups.append(
            {
                "stage_name": group["stage_name"],
                "model_name": group["model_name"],
                "call_count": len(group["input_tokens"]),
                "input_tokens": input_summary,
                "output_tokens": _summarize(group["output_tokens"]),
                "cache_read_input_tokens": group["cache_read_input_tokens"],
                "cache_creation_input_tokens": group["cache_creation_input_tokens"],
                "cache_read_share": (
                    group["cache_read_input_tokens"] / input_sum if input_sum else 0.0
                ),
            }
        )

    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "window_seconds": (until - since).total_seconds(),
        "record_count": len(rows),
        "groups": result_groups,
    }
