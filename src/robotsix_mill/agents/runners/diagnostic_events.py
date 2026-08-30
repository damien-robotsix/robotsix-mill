"""Diagnostic event store for CI failures and other recurring categories.

Provides a lightweight JSONL-based event store (one file per repo under
``<data_dir>/<board_id>/diagnostic_events.jsonl``) and the emit/list
functions consumed by the ci-fix stage and the recurring-category
diagnostic check.

Events are deduplicated on ``(category, ticket_id, normalized_key)`` so a
single stuck ticket retrying the same failure many times does not flood
the category.

``CI_FAILURE`` events (and their ``CI_FIX_RESOLVED`` counterparts, emitted
when the ci-fix agent turns CI green) additionally carry a semantic
``bucket`` / ``root_cause`` / ``prevention_rule`` triple derived by
:mod:`robotsix_mill.stages.ci_failure_buckets`. The fields are optional so
events stored before they existed still load (they read back as ``""``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ...config import Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DiagnosticEvent:
    """A single diagnostic event stored in the JSONL event store.

    Attributes:
        category: The event category (e.g. ``"CI_FAILURE"``).
        ticket_id: The ticket that triggered the event.
        repo_id: The repository/board id where the event occurred.
        reason: Human-readable failure reason.
        normalized_key: Stable, deterministic key for clustering
            recurring failures (e.g. first 16 hex digits of a SHA-256
            hash of the structured failure summary).
        timestamp: ISO-8601 UTC timestamp of when the event was emitted.
        bucket: Semantic failure bucket (``"ruff-format"``, ``"mypy"``, …);
            ``""`` for events stored before buckets existed.
        root_cause: One-line root cause picked from the failing log.
        prevention_rule: Imperative rule that would have prevented the
            failure (deterministic seed for the ``ci_prevention_rules``
            pass); ``""`` when none is known.
    """

    category: str
    ticket_id: str
    repo_id: str
    reason: str
    normalized_key: str
    timestamp: str
    bucket: str = ""
    root_cause: str = ""
    prevention_rule: str = ""


def _events_file_path(settings: Settings, board_id: str) -> Path:
    """Resolve the JSONL event-store path for *board_id*."""
    return settings.diagnostic_events_file_for(board_id)


def emit_diagnostic_event(
    settings: Settings,
    board_id: str,
    category: str,
    ticket_id: str,
    reason: str,
    normalized_key: str,
    *,
    bucket: str = "",
    root_cause: str = "",
    prevention_rule: str = "",
) -> bool:
    """Append a diagnostic event to the per-repo JSONL store.

    Deduplicates on ``(category, ticket_id, normalized_key)``: if an
    event with the same category, ticket and normalized key already
    exists in the store, the new event is silently skipped and ``False``
    is returned.  Otherwise the event is appended and ``True`` is
    returned.

    Fail-safe: any I/O error is logged and ``False`` is returned (the
    caller must not break on a failed event write).
    """
    try:
        path = _events_file_path(settings, board_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Dedup: check for an existing (category, ticket_id, normalized_key).
        if _event_exists(path, ticket_id, normalized_key, category):
            log.debug(
                "diagnostic_events: skipping duplicate event "
                "ticket=%s category=%s key=%s",
                ticket_id,
                category,
                normalized_key,
            )
            return False

        event = DiagnosticEvent(
            category=category,
            ticket_id=ticket_id,
            repo_id=board_id,
            reason=reason,
            normalized_key=normalized_key,
            timestamp=datetime.now(UTC).isoformat(),
            bucket=bucket,
            root_cause=root_cause,
            prevention_rule=prevention_rule,
        )
        payload: dict[str, str] = {
            "category": event.category,
            "ticket_id": event.ticket_id,
            "repo_id": event.repo_id,
            "reason": event.reason,
            "normalized_key": event.normalized_key,
            "timestamp": event.timestamp,
        }
        # Only write the semantic fields when set, so plain events keep
        # the historical line shape.
        for key in ("bucket", "root_cause", "prevention_rule"):
            value = getattr(event, key)
            if value:
                payload[key] = value
        line = json.dumps(payload, ensure_ascii=False)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        log.info(
            "diagnostic_events: emitted event category=%s ticket=%s key=%s",
            category,
            ticket_id,
            normalized_key,
        )
        return True
    except Exception:
        log.exception(
            "diagnostic_events: failed to emit event category=%s ticket=%s",
            category,
            ticket_id,
        )
        return False


def _parse_event_line(obj: dict[str, Any]) -> DiagnosticEvent | None:
    """Parse a JSONL object into a :class:`DiagnosticEvent`.

    Returns ``None`` if required keys are missing or values are invalid.
    """
    try:
        return DiagnosticEvent(
            category=str(obj["category"]),
            ticket_id=str(obj["ticket_id"]),
            repo_id=str(obj.get("repo_id", "")),
            reason=str(obj.get("reason", "")),
            normalized_key=str(obj["normalized_key"]),
            timestamp=str(obj.get("timestamp", "")),
            bucket=str(obj.get("bucket", "") or ""),
            root_cause=str(obj.get("root_cause", "") or ""),
            prevention_rule=str(obj.get("prevention_rule", "") or ""),
        )
    except KeyError, TypeError, ValueError:
        return None


def _is_stale(ev: DiagnosticEvent, cutoff: datetime) -> bool:
    """Return ``True`` if *ev*'s timestamp is older than *cutoff*.

    A malformed timestamp is *not* considered stale — we'd rather
    surface a suspicious event than silently drop it.
    """
    if not ev.timestamp:
        return False
    try:
        return datetime.fromisoformat(ev.timestamp) < cutoff
    except ValueError:
        return False


def list_diagnostic_events(
    settings: Settings,
    board_id: str,
    *,
    category: str | None = None,
) -> list[DiagnosticEvent]:
    """Return all diagnostic events for *board_id*, optionally filtered.

    Reads the JSONL file line by line; silently skips malformed lines
    and returns an empty list when the file does not exist.

    Events older than ``settings.diagnostic_events_max_age_days`` days
    are silently dropped (aging).  A setting of 0 disables aging and
    returns all events (original behaviour).

    Args:
        settings: Resolved settings for path derivation.
        board_id: The repo/board whose events to list.
        category: When set, return only events matching this category.
    """
    try:
        path = _events_file_path(settings, board_id)
        if not path.is_file():
            return []
        max_age_days = settings.diagnostic_events_max_age_days
        now = datetime.now(UTC)
        cutoff: datetime | None = (
            now - timedelta(days=max_age_days) if max_age_days > 0 else None
        )
        events: list[DiagnosticEvent] = []
        for line in path.read_text("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                log.warning("diagnostic_events: skipping malformed line in %s", path)
                continue
            ev = _parse_event_line(obj)
            if ev is None:
                log.warning("diagnostic_events: skipping invalid entry in %s", path)
                continue
            # Age-filter: drop events whose timestamp is older than the cutoff.
            if cutoff is not None and _is_stale(ev, cutoff):
                continue
            if category is not None and ev.category != category:
                continue
            events.append(ev)
        return events
    except Exception:
        log.exception("diagnostic_events: failed to list events for board %s", board_id)
        return []


def _event_exists(
    path: Path, ticket_id: str, normalized_key: str, category: str
) -> bool:
    """Return ``True`` if an event with the same category+ticket+key exists."""
    try:
        if not path.is_file():
            return False
        for line in path.read_text("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                str(obj.get("ticket_id", "")) == ticket_id
                and str(obj.get("normalized_key", "")) == normalized_key
                and str(obj.get("category", "")) == category
            ):
                return True
    except Exception:
        log.warning("diagnostic_events: dedup check failed for %s", path, exc_info=True)
    return False
