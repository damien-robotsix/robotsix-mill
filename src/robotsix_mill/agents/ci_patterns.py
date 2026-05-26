"""Structured CI pattern memory for the ci-fix agent.

Stores categorized fix attempts with success/failure stats in a JSON
file, enabling the agent to look up proven approaches for recurring
failure signatures without re-reading the entire Markdown ledger.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import BaseModel

log = logging.getLogger(__name__)

_MAX_ENTRIES = 50


class CiPatternEntry(BaseModel):
    """A single CI-fix attempt, indexed by failure category and signature."""

    category: str
    signature: str
    approach: str
    success: bool
    attempts: int
    ticket_id: str
    timestamp: str  # ISO-8601 UTC


def load_patterns(path: Path) -> list[CiPatternEntry]:
    """Load pattern entries from *path*, returning [] on any error."""
    try:
        raw = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        log.debug("ci_patterns: could not load %s", path, exc_info=True)
        return []
    entries: list[CiPatternEntry] = []
    for item in raw:
        try:
            entries.append(CiPatternEntry(**item))
        except Exception:
            log.warning("ci_patterns: skipping invalid entry %r", item)
    return entries


def save_patterns(path: Path, patterns: list[CiPatternEntry]) -> None:
    """Persist *patterns* to *path*, trimming to the most recent 50."""
    path.parent.mkdir(parents=True, exist_ok=True)
    trimmed = patterns[-_MAX_ENTRIES:]
    payload = json.dumps([p.model_dump() for p in trimmed], indent=2)
    path.write_text(payload, "utf-8")


def find_relevant_patterns(
    patterns: list[CiPatternEntry],
    failing_summary: str,
    *,
    category: str | None = None,
    limit: int = 3,
) -> list[CiPatternEntry]:
    """Return recent patterns whose signature appears in *failing_summary*.

    Matching is case-insensitive substring.  When *category* is not
    ``None``, only entries with a matching category are considered.
    Results are sorted by ``timestamp`` descending and capped at *limit*.
    """
    summary_lower = failing_summary.lower()
    candidates = patterns
    if category is not None:
        candidates = [p for p in candidates if p.category == category]
    matches = [p for p in candidates if p.signature.lower() in summary_lower]
    matches.sort(key=lambda p: p.timestamp, reverse=True)
    return matches[:limit]
