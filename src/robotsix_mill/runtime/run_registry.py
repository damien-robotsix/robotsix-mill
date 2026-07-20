"""Run registry — durable, thread-safe record of audit/trace-health runs."""

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
    """A single run entry in the registry.

    Each entry represents one background run (audit, health check, survey, etc.)
    with its lifecycle tracked via ``status`` (running → ok|error).  Entries
    are persisted as JSON and loaded on startup, where ``running`` entries
    from a prior crash are reconciled to ``error``.

    Attributes:
        id: Unique hex identifier (``uuid.uuid4().hex``).
        kind: Labels matching ``registry.start(\"...\")`` call sites. Both
            hyphenated and underscored forms are accepted for backward
            compatibility (e.g. ``\"bc-check\"`` and ``\"bc_check\"``).
        started_at: ISO-8601 UTC timestamp of when the run started.
        finished_at: ISO-8601 UTC timestamp of when the run finished, or
            ``None`` for in-flight entries.
        status: One of ``\"running\"``, ``\"ok\"``, ``\"error\"``.
        summary: Human-readable summary of the run's outcome.
        error: Error detail when ``status == \"error\"``, else ``None``.
        repo_id: Scoping repo identifier for per-repo periodic runs;
            ``\"\"`` for legacy global entries.
    """

    id: str
    # Mirrors every label string passed to ``registry.start(label)``.
    # The codebase mixes hyphens (older callers) with underscores
    # (newer callers); both styles are intentionally accepted here
    # rather than forcing a global rename. Keep this list in sync
    # with grep ``registry.start("``.
    kind: Literal[
        "agent_check",
        "audit",
        "bc-check",
        "bc_check",
        "completeness-check",
        "completeness_check",
        "config-sync",
        "copy-paste",
        "copy_paste",
        "data-dir-gc",
        "data_dir_gc",
        "diagnostic",
        "epic-breakdown",
        "forge-parity",
        "forge_parity",
        "health",
        "langfuse-cleanup",
        "member-sync",
        "member_sync",
        "meta",
        "module_curator",
        "roadmap-sync",
        "run-health",
        "run_health",
        "state-sync",
        "state_sync",
        "survey",
        "test-gap",
        "trace-health",
        "trace-review",
        "trace_review",
    ]
    started_at: str  # ISO-8601 UTC
    finished_at: str | None = None
    status: Literal["running", "ok", "error"] = "running"
    summary: str = ""
    error: str | None = None
    repo_id: str = ""


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
                    repo_id=e.get("repo_id", ""),
                )
                for e in data
            ]
        except json.JSONDecodeError, KeyError:
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

    def start(self, kind: str, repo_id: str = "") -> str:
        """Create a ``"running"`` entry, persist, and return its id."""
        with self._lock:
            entry = RunEntry(
                id=uuid.uuid4().hex,
                kind=kind,  # type: ignore[arg-type]  # validated by callers
                started_at=datetime.now(timezone.utc).isoformat(),
                status="running",
                repo_id=repo_id,
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

    def most_recent(
        self,
        kind: str,
        repo_id: str | None = None,
    ) -> dict | None:
        """Return the newest successful entry of the given *kind*.

        Only ``status == "ok"`` counts as "ran" for the periodic
        scheduler's purposes — an interrupted-by-restart entry or
        an errored run hasn't actually executed the work, so the
        next fire window should be measured from the last successful
        run instead of bumping forward on every crash.

        When *repo_id* is given, only entries with a matching
        ``repo_id`` are considered. ``repo_id=None`` (default) keeps
        the legacy any-repo behaviour for non-per-repo callers.
        """
        with self._lock:
            for e in reversed(self._entries):
                if e.kind != kind or e.status != "ok":
                    continue
                if repo_id is not None and e.repo_id != repo_id:
                    continue
                return asdict(e)
            return None

    def list_all(self) -> list[dict]:
        """Return all entries as dicts, newest first (includes running)."""
        with self._lock:
            return [asdict(e) for e in reversed(self._entries)]
