"""Run registry — durable, thread-safe record of audit/scout/trace-health runs."""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

MAX_ENTRIES = 50


@dataclass
class RunEntry:
    id: str
    kind: Literal["audit", "scout", "trace-health", "health"]
    started_at: str  # ISO-8601 UTC
    finished_at: str | None = None
    status: Literal["running", "ok", "error"] = "running"
    summary: str = ""
    error: str | None = None


class RunRegistry:
    """In-memory + file-backed registry of background run results.

    Thread-safe: a ``threading.Lock`` guards all reads/writes to both
    the in-memory list and the JSON file.  Capped at ``MAX_ENTRIES``
    most-recent entries on each save.
    """

    def __init__(self, file_path: Path) -> None:
        self._file = file_path
        self._lock = threading.Lock()
        self._entries: list[RunEntry] = []
        self._load()

    # -- persistence -------------------------------------------------

    def _load(self) -> None:
        if not self._file.exists():
            return
        try:
            data = json.loads(self._file.read_text())
            self._entries = [
                RunEntry(
                    id=e["id"],
                    kind=e["kind"],
                    started_at=e["started_at"],
                    finished_at=e.get("finished_at"),
                    status=e.get("status", "running"),
                    summary=e.get("summary", ""),
                    error=e.get("error"),
                )
                for e in data
            ]
        except (json.JSONDecodeError, KeyError):
            self._entries = []
            return

        # Reconcile orphaned "running" entries: any pass that was
        # in-flight when the process previously stopped (container
        # restart, crash, OOM, kill) is now permanently dead — the
        # background thread that would have called finish_ok/error
        # died with the process. Mark them errored so they don't
        # hang as "running" in the board forever. Persist so we
        # only do this once per restart.
        now = datetime.now(timezone.utc).isoformat()
        reconciled = False
        for e in self._entries:
            if e.status == "running":
                e.status = "error"
                e.finished_at = now
                e.error = "interrupted by process restart"
                reconciled = True
        if reconciled:
            self.flush()

    def flush(self) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        if len(self._entries) > MAX_ENTRIES:
            self._entries = self._entries[-MAX_ENTRIES:]
        data = [asdict(e) for e in self._entries]
        self._file.write_text(json.dumps(data, default=str))

    # -- public API --------------------------------------------------

    def start(self, kind: str) -> str:
        """Create a ``"running"`` entry, persist, and return its id."""
        with self._lock:
            entry = RunEntry(
                id=uuid.uuid4().hex,
                kind=kind,  # type: ignore[arg-type]  # validated by callers
                started_at=datetime.now(timezone.utc).isoformat(),
                status="running",
            )
            self._entries.append(entry)
            self.flush()
            return entry.id

    def finish_ok(self, run_id: str, summary: str) -> None:
        """Mark *run_id* as ``"ok"`` with a human-readable *summary*."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            for e in self._entries:
                if e.id == run_id:
                    e.status = "ok"
                    e.finished_at = now
                    e.summary = summary
                    break
            self.flush()

    def finish_error(self, run_id: str, error: str) -> None:
        """Mark *run_id* as ``"error"`` with an error string."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            for e in self._entries:
                if e.id == run_id:
                    e.status = "error"
                    e.finished_at = now
                    e.error = error
                    break
            self.flush()

    def list_all(self) -> list[dict]:
        """Return all entries as dicts, newest first (includes running)."""
        with self._lock:
            return [asdict(e) for e in reversed(self._entries)]
