"""Thread-safe, file-backed store for LLM conversation messages captured
during a ticket's pipeline run.

See ``DeepReviewStore`` for the locking / atomic-write patterns this
module mirrors.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


class ContextStore:
    """Persists captured LLM conversation turns as per-conversation JSON files.

    - Lazy-loads conversation files on first access.
    - Atomic writes via tmp-file + ``os.replace``.
    - Recovers gracefully from corrupt files.
    - Thread-safe via ``threading.Lock``.
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._lock = threading.Lock()
        self._cache: dict[str, list[dict]] = {}

    def _file_path(self, conversation_id: str) -> Path:
        return self._base_dir / f"{conversation_id}.json"

    def _ensure_loaded(self, conversation_id: str) -> list[dict]:
        """Lazy-load conversation turns from disk. Returns the list
        (possibly empty)."""
        if conversation_id in self._cache:
            return self._cache[conversation_id]
        file_path = self._file_path(conversation_id)
        if not file_path.exists():
            self._cache[conversation_id] = []
            return []
        try:
            raw = file_path.read_text(encoding="utf-8")
            turns = json.loads(raw)
            if isinstance(turns, list):
                self._cache[conversation_id] = turns
            else:
                log.warning(
                    "ContextStore: %s is not a JSON list — "
                    "discarding and starting empty",
                    file_path,
                )
                self._cache[conversation_id] = []
        except (json.JSONDecodeError, OSError) as e:
            log.warning(
                "ContextStore: could not parse %s (%s) — "
                "starting empty; next append_messages() will overwrite",
                file_path,
                e,
            )
            self._cache[conversation_id] = []
        return self._cache[conversation_id]

    def _flush(self, conversation_id: str) -> None:
        """Write cached turns to the conversation file atomically.

        Must be called while ``self._lock`` is held.
        """
        turns = self._cache.get(conversation_id, [])
        file_path = self._file_path(conversation_id)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = file_path.with_suffix(".json.tmp")
        content = json.dumps(turns, default=str, ensure_ascii=False)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, file_path)

    # -- public API --------------------------------------------------------

    def append_messages(self, conversation_id: str, messages_json: str) -> None:
        """Append a conversation turn to the store.

        Lazy-loads the conversation file from disk, appends a turn
        object with an auto-incremented index and UTC timestamp, then
        atomically writes back to disk.
        """
        with self._lock:
            turns = self._ensure_loaded(conversation_id)
            next_index = len(turns)
            turn: dict = {
                "index": next_index,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "messages_json": messages_json,
            }
            turns.append(turn)
            self._cache[conversation_id] = turns
            self._flush(conversation_id)

    def delete_conversation(self, conversation_id: str) -> None:
        """Remove the conversation from cache and disk.

        Silently succeeds when the conversation doesn't exist
        (idempotent).  Swallows ``OSError`` after logging a warning,
        matching ``prune_clone``'s error-handling posture.
        Thread-safe: acquires ``self._lock`` while mutating state.
        """
        with self._lock:
            self._cache.pop(conversation_id, None)
            file_path = self._file_path(conversation_id)
            try:
                file_path.unlink(missing_ok=True)
            except OSError as exc:
                log.warning(
                    "ContextStore.delete_conversation: could not remove %s (%s)",
                    file_path,
                    exc,
                )

    def get_messages(self, conversation_id: str) -> list[dict]:
        """Return all conversation turns sorted by index ascending.

        Returns an empty list if the conversation file doesn't exist.
        """
        with self._lock:
            turns = self._ensure_loaded(conversation_id)
            return list(turns)


# -- module-level context vars -------------------------------------------

_current_context_store: ContextVar[ContextStore | None] = ContextVar(
    "_current_context_store", default=None
)
_current_conversation_id: ContextVar[str | None] = ContextVar(
    "_current_conversation_id", default=None
)
