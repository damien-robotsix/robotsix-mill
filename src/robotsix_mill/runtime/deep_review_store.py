"""Thread-safe, file-backed store for completed deep-review results.

See ``RunRegistry`` for the locking / atomic-write patterns this
module mirrors.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


class DeepReviewStore:
    """Persists completed deep-review results in a JSON file on disk.

    - Lazy-loads on first access (not at ``__init__``).
    - Atomic writes via tmp-file + ``os.replace``.
    - Capped at ``MAX_ENTRIES`` (20 newest by ``finished_at``).
    - Recovers gracefully from a corrupt file.
    - Thread-safe via ``threading.Lock``.
    """

    MAX_ENTRIES = 20

    def __init__(self, file_path: Path) -> None:
        self._file_path = file_path
        self._lock = threading.Lock()
        self._entries: list[dict] = []
        self._loaded = False

    # -- internal helpers --------------------------------------------------

    def _ensure_loaded(self) -> None:
        """Lazy-load from disk on first access."""
        if self._loaded:
            return
        self._loaded = True
        if not self._file_path.exists():
            return
        try:
            raw = self._file_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, list):
                self._entries = data
            else:
                log.warning(
                    "deep_review_store: %s is not a JSON list — "
                    "discarding and starting empty",
                    self._file_path,
                )
        except (json.JSONDecodeError, OSError) as e:
            log.warning(
                "deep_review_store: could not parse %s (%s) — "
                "starting empty; next put() will overwrite",
                self._file_path,
                e,
            )

    def _flush(self) -> None:
        """Write ``_entries`` to ``_file_path`` atomically.

        Must be called while ``self._lock`` is held.
        """
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file_path.with_suffix(".json.tmp")
        content = json.dumps(self._entries, default=str, ensure_ascii=False)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._file_path)

    # -- public API --------------------------------------------------------

    def put(self, trace_id: str, entry: dict) -> None:
        """Atomically write *entry* to the JSON file.

        - Adds ``finished_at`` (ISO-8601 UTC, generated now).
        - Overwrites any existing entry for the same ``trace_id``.
        - Prunes to ``MAX_ENTRIES`` newest by ``finished_at``.
        """
        finished_at = datetime.now(timezone.utc).isoformat()
        entry = {**entry, "finished_at": finished_at}

        with self._lock:
            self._ensure_loaded()
            # Remove existing entry for this trace_id (overwrite).
            self._entries = [
                e for e in self._entries if e.get("trace_id") != trace_id
            ]
            self._entries.append(entry)
            # Sort newest-first by finished_at.
            self._entries.sort(
                key=lambda e: e.get("finished_at", ""), reverse=True
            )
            # Prune to MAX_ENTRIES.
            self._entries = self._entries[: self.MAX_ENTRIES]
            self._flush()

    def get(self, trace_id: str) -> dict | None:
        """Return the stored entry (with ``finished_at``) or ``None``."""
        with self._lock:
            self._ensure_loaded()
            for e in self._entries:
                if e.get("trace_id") == trace_id:
                    return e
        return None

    def list_all(self) -> list[dict]:
        """Return all stored entries, newest by ``finished_at`` first."""
        with self._lock:
            self._ensure_loaded()
            return list(self._entries)
