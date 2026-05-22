"""File-backed typed key-value store for transient cross-stage context.

Provides a lightweight :class:`ContextStore` that survives process
restarts — components can stash and retrieve typed data without coupling
to the filesystem workspace, polluting the DB schema, or relying on
external storage.

Persistence follows the atomic-write pattern established by
:class:`DeepReviewStore`; thread-safety mirrors :class:`RunRegistry`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Literal

ContextType = Literal["conversation", "file"]

log = logging.getLogger(__name__)


class ContextStore:
    """File-backed typed key-value store that survives process restarts.

    Data is stored in a JSON file on disk and eagerly loaded at
    construction.  Every :meth:`store` call immediately flushes to disk
    using an atomic write (tmp-file + ``os.fsync`` + ``os.replace``).

    Thread-safe via ``threading.Lock``.  Each entry is keyed by the
    compound ``(key, type)`` — the same *key* with different *type*
    values are independent slots.

    There is no size cap or eviction policy; the store grows unbounded.
    Callers that need pruning should add it later (YAGNI).
    """

    def __init__(self, file_path: Path) -> None:
        """*file_path* — path to the JSON backing file (e.g.
        ``settings.data_dir / "context_store.json"``).

        Loads existing data eagerly.  A missing or corrupt file is
        treated as an empty store (a warning is logged for corruption).
        """
        self._file_path = file_path
        self._lock = threading.Lock()
        self._data: dict[tuple[str, str], Any] = {}
        self._load()
        # Ensure the backing file always exists after construction
        # (creates parent dirs + writes empty JSON if missing).
        if not self._file_path.exists():
            self._flush()

    # -- persistence helpers -----------------------------------------------

    def _load(self) -> None:
        """Eagerly load ``_data`` from the JSON backing file.

        Converts the on-disk ``{type: {key: data}}`` structure into
        flat ``(key, type)`` tuple keys for O(1) in-memory lookup.

        A missing or corrupt file is treated as an empty store;
        the next :meth:`store` will atomically overwrite the file.
        """
        if not self._file_path.exists():
            return
        try:
            raw = self._file_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (json.JSONDecodeError, OSError) as e:
            log.warning(
                "context_store: could not parse %s (%s) — "
                "starting empty; next store() will overwrite",
                self._file_path,
                e,
            )
            return

        if not isinstance(data, dict):
            log.warning(
                "context_store: %s is not a JSON object — "
                "discarding and starting empty",
                self._file_path,
            )
            return

        # Convert {type: {key: value}} → {(key, type): value}
        for type_name, entries in data.items():
            if not isinstance(entries, dict):
                continue
            for key, value in entries.items():
                self._data[(key, type_name)] = value

    def _flush(self) -> None:
        """Write ``_data`` to ``_file_path`` atomically.

        Must be called while ``self._lock`` is held.

        Converts the flat ``(key, type)`` in-memory dict into the
        nested ``{type: {key: data}}`` on-disk structure.
        """
        # Build nested structure
        nested: dict[str, dict[str, Any]] = {}
        for (key, type_name), value in self._data.items():
            nested.setdefault(type_name, {})[key] = value

        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file_path.with_suffix(".json.tmp")
        content = json.dumps(nested, default=str, ensure_ascii=False)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._file_path)

    # -- public API --------------------------------------------------------

    def store(self, key: str, type: ContextType, data: Any) -> None:
        """Store *data* under (*key*, *type*). Overwrites any previous
        value and immediately persists to disk.

        Args:
            key: An arbitrary string identifier (e.g. a ticket id, a
                stage name, or any caller-chosen namespace).
            type: The kind of data being stored — ``"conversation"``
                for LLM message history, ``"file"`` for file contents.
            data: Any **JSON-serializable** Python object.  Values that
                are not natively serializable are converted via
                ``str()`` as a fallback (matching the
                ``json.dumps(default=str)`` convention used by
                :class:`RunRegistry`).
        """
        with self._lock:
            self._data[(key, type)] = data
            self._flush()

    def retrieve(self, key: str, type: ContextType) -> Any | None:
        """Return the data stored under (*key*, *type*), or ``None``.

        Args:
            key: The key originally passed to :meth:`store`.
            type: The type originally passed to :meth:`store`.

        Returns:
            The stored object (deserialized from JSON — typically
            dicts, lists, strings, numbers, bools, or ``None``), or
            ``None`` if no entry exists for that compound key.
        """
        with self._lock:
            return self._data.get((key, type))
