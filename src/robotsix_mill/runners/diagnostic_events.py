"""Diagnostic event store for CI failures and other recurring categories.

Provides a lightweight JSONL-based event store (one file per repo under
``<data_dir>/<board_id>/diagnostic_events.jsonl``) and the emit/list
functions consumed by the ci-fix stage and the recurring-category
diagnostic check.

Events are deduplicated on ``(ticket_id, normalized_key)`` so a single
stuck ticket retrying the same failure many times does not flood the
category.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..config import Settings

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
    """

    category: str
    ticket_id: str
    repo_id: str
    reason: str
    normalized_key: str
    timestamp: str


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
) -> bool:
    """Append a diagnostic event to the per-repo JSONL store.

    Deduplicates on ``(ticket_id, normalized_key)``: if an event with
    the same ticket and normalized key already exists in the store, the
    new event is silently skipped and ``False`` is returned.  Otherwise
    the event is appended and ``True`` is returned.

    Fail-safe: any I/O error is logged and ``False`` is returned (the
    caller must not break on a failed event write).
    """
    try:
        path = _events_file_path(settings, board_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Dedup: check for existing (ticket_id, normalized_key) pair.
        if _event_exists(path, ticket_id, normalized_key):
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
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        line = json.dumps(
            {
                "category": event.category,
                "ticket_id": event.ticket_id,
                "repo_id": event.repo_id,
                "reason": event.reason,
                "normalized_key": event.normalized_key,
                "timestamp": event.timestamp,
            },
            ensure_ascii=False,
        )
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        log.info(
            "diagnostic_events: emitted event category=%s ticket=%s key=%s",
            category,
            ticket_id,
            normalized_key,
        )
        return True
    except Exception:  # noqa: BLE001 — event write must not crash the stage
        log.exception(
            "diagnostic_events: failed to emit event category=%s ticket=%s",
            category,
            ticket_id,
        )
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

    Args:
        settings: Resolved settings for path derivation.
        board_id: The repo/board whose events to list.
        category: When set, return only events matching this category.
    """
    try:
        path = _events_file_path(settings, board_id)
        if not path.is_file():
            return []
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
            try:
                ev = DiagnosticEvent(
                    category=str(obj["category"]),
                    ticket_id=str(obj["ticket_id"]),
                    repo_id=str(obj.get("repo_id", "")),
                    reason=str(obj.get("reason", "")),
                    normalized_key=str(obj["normalized_key"]),
                    timestamp=str(obj.get("timestamp", "")),
                )
            except KeyError, TypeError, ValueError:
                log.warning("diagnostic_events: skipping invalid entry in %s", path)
                continue
            if category is not None and ev.category != category:
                continue
            events.append(ev)
        return events
    except Exception:  # noqa: BLE001 — data read must not crash callers
        log.exception("diagnostic_events: failed to list events for board %s", board_id)
        return []


def _event_exists(path: Path, ticket_id: str, normalized_key: str) -> bool:
    """Return ``True`` if an event with the same ticket+key already exists."""
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
            ):
                return True
    except Exception:  # noqa: BLE001 — fail open (don't block emission)
        log.warning("diagnostic_events: dedup check failed for %s", path, exc_info=True)
    return False
