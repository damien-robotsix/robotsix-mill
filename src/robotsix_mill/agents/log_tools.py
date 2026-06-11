"""Tool factory for querying a managed repo's deployed application logs.

The refine agent gets an interactive ``query_app_logs`` tool — built by
:func:`make_log_query_tool` — only when the target repo declares a
``deployed_log_folder`` in mill's central ``config/repos.yaml`` *and* that
folder resolves to an existing directory in the sandbox.  It complements
the static ``## Deployed system logs`` directory listing injected into the
system prompt: the summary orients the agent, this tool lets it drill into
specific log lines with keyword and recency filtering instead of blindly
``read_file``-ing whole (possibly rotated) log files.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Mirror the deployed-log-summary traversal: skip files whose extension is
# a known binary type so the tool never dumps binary noise.
_BINARY_EXTENSIONS = frozenset(
    {
        ".gz",
        ".zip",
        ".tar",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".pkl",
        ".pickle",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".svg",
        ".ico",
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".mp3",
        ".mp4",
        ".wav",
        ".avi",
        ".mov",
        ".pyc",
        ".pyo",
        ".so",
        ".dll",
        ".exe",
        ".bin",
        ".dat",
        ".elf",
    }
)

# Per-file read cap so a single huge rotated log can't blow up memory: we
# only ever keep this many trailing lines from each file in the window.
_MAX_LINES_PER_FILE = 5000


def make_log_query_tool(log_dir: Path):  # noqa: C901 — nested closure's recency/keyword/truncation branches inflate the count
    """Build the ``query_app_logs`` tool closure bound to *log_dir*.

    Registers the tool in :class:`ToolRegistry` at construction time
    (mirroring ``langfuse_tools.py``) and returns a plain callable whose
    docstring becomes the tool description.  The closure never raises —
    a missing/empty/unreadable folder yields a short explanatory string.
    """

    def query_app_logs(  # noqa: C901 — sequential recency/keyword/truncation filters, not deep nesting
        keywords: str = "", since_hours: int = 24, max_lines: int = 200
    ) -> str:
        """Query the managed repo's deployed application logs.

        Searches the configured deployed log folder for recent log lines,
        with keyword and recency filtering — use it to find actual
        errors/warnings (ingestion, IMAP, pipeline, …) when refining a
        ticket about runtime behaviour, instead of reading whole rotated
        log files.

        Parameters
        ----------
        keywords:
            Space-separated terms. When non-empty, only lines containing
            at least one term (case-insensitive, OR'd) are returned. When
            empty, the most recent ``max_lines`` lines across in-window
            files are returned.
        since_hours:
            Only consider files whose modification time falls within this
            many hours (default 24). Recency is gated at the *file* level
            (log-line timestamp formats vary and are not reliably
            parseable); stale files are skipped entirely.
        max_lines:
            Cap on the number of returned lines (default 200). When the
            cap trims matches, a trailing ``... (truncated, N more
            matching lines)`` marker is appended.

        Returns a plain-text block of matching log lines (each prefixed
        with its source file name), or a short explanatory string when
        nothing matches or the folder is missing/empty.
        """
        try:
            if not log_dir.is_dir():
                return f"Deployed log folder '{log_dir}' is missing or not a directory."

            try:
                cutoff = datetime.now(tz=timezone.utc).timestamp() - since_hours * 3600
            except OverflowError, ValueError:
                cutoff = 0.0

            terms = [t for t in keywords.lower().split() if t]

            # Collect in-window text files, newest first.
            candidates: list[tuple[float, Path]] = []
            for entry in sorted(log_dir.rglob("*")):
                if not entry.is_file():
                    continue
                if entry.suffix.lower() in _BINARY_EXTENSIONS:
                    continue
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                if mtime < cutoff:
                    continue
                candidates.append((mtime, entry))

            if not candidates:
                return (
                    f"No log files modified in the last {since_hours}h "
                    f"under '{log_dir}'."
                )

            candidates.sort(key=lambda c: c[0], reverse=True)

            # Collect newest-first: candidates are sorted newest file first,
            # and within each file the most recent lines are at the end, so we
            # iterate each file's window back-to-front. This keeps the genuinely
            # recent lines and counts the older overflow as truncated — the
            # opposite of iterating front-to-back, which would surface a file's
            # oldest lines and drop its recent ones.
            collected: list[str] = []
            extra = 0
            for _mtime, entry in candidates:
                rel = entry.relative_to(log_dir)
                try:
                    with open(entry, "r", encoding="utf-8", errors="replace") as f:
                        file_lines = deque(f, maxlen=_MAX_LINES_PER_FILE)
                except OSError:
                    continue
                for raw in reversed(file_lines):
                    line = raw.rstrip("\n")
                    if terms and not any(t in line.lower() for t in terms):
                        continue
                    if len(collected) >= max_lines:
                        extra += 1
                        continue
                    collected.append(f"{rel}: {line}")

            if not collected and extra == 0:
                if terms:
                    return (
                        f"No lines matching {keywords!r} in log files from the "
                        f"last {since_hours}h."
                    )
                return f"No log lines found in files from the last {since_hours}h."

            # ``collected`` is newest-first; present it chronologically
            # (oldest of the recent window first, most recent last).
            collected.reverse()

            out = "\n".join(collected)
            if extra:
                label = "matching lines" if terms else "lines"
                out += f"\n... (truncated, {extra} more {label})"
            return out
        except Exception as exc:  # never raise out of a tool closure
            log.warning("query_app_logs failed: %s", exc)
            return f"Could not read deployed logs: {exc}"

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="query_app_logs",
            description=(
                "Query the managed repo's deployed application logs with "
                "keyword (case-insensitive, space-separated OR'd) and "
                "recency (since_hours) filtering, truncated to max_lines."
            ),
            category="reporting",
            parameters={
                "keywords": "str",
                "since_hours": "int",
                "max_lines": "int",
            },
        )
    )

    return query_app_logs
