"""Deterministic outstanding-TODO scanner for the meta-agent.

The cross-repo meta-agent used to *discover* ``TODO``/``FIXME``/``XXX``/
``HACK`` markers by grepping each clone itself via the ``explore`` tool.
That discovery was non-deterministic — coverage varied pass to pass and
the model sometimes skipped clones or invented markers. This module
replaces it with an in-code scan whose result is injected into the
prompt as the authoritative ``<outstanding-todos>`` section; the model's
job is reduced to *judging relevance* and confirming with ``read_file``.

The scan uses ``git grep`` so it searches **tracked files only**
(untracked/``.gitignore``d files are excluded by design), skips ``.git/``
and binary files, and is fast and reproducible.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Whole-word, case-sensitive marker tokens. Lowercase ``todo`` does NOT match.
MARKERS = ("TODO", "FIXME", "XXX", "HACK")

# Per-repo and global caps. Kept as module constants so the scan defaults
# and the ``format_outstanding_todos`` truncation note stay in sync.
MAX_PER_REPO = 100
MAX_TOTAL = 300

# Case-sensitive, whole-word alternation. ``re.search`` returns the
# leftmost match, so the captured group is the first marker on the line.
_MARKER_RE = r"\b(TODO|FIXME|XXX|HACK)\b"

# Leading characters stripped from a matched line to recover the marker
# text: surrounding whitespace plus common comment-lead punctuation
# (``#``, ``//``, ``/*``, ``<!--``, ``--``, ``;``).
_COMMENT_LEAD = " \t#/*<!->;"

_TEXT_CAP = 200


@dataclass(frozen=True)
class TodoMarker:
    """A single outstanding marker found in a tracked file.

    ``path`` is POSIX-style and relative to the clone root, ``line`` is
    1-indexed, ``marker`` is one of :data:`MARKERS`, and ``text`` is the
    matched line trimmed of leading comment punctuation/whitespace and
    capped at 200 chars.
    """

    repo_id: str
    path: str
    line: int
    marker: str
    text: str


def _first_marker(line: str) -> str | None:
    """Return the first marker token appearing on *line*, or ``None``."""
    m = re.search(_MARKER_RE, line)
    return m.group(1) if m else None


def _trim(content: str) -> str:
    """Strip leading comment punctuation/whitespace and cap to 200 chars."""
    return content.strip().lstrip(_COMMENT_LEAD).strip()[:_TEXT_CAP]


def _scan_clone(repo_id: str, clone: Path) -> list[TodoMarker]:
    """Scan a single clone with ``git grep`` (tracked files only).

    Returns the parsed markers. A clone that is not a git repo (or any
    other ``git grep`` failure) is logged at ``warning`` and skipped —
    the pass must never crash. ``git grep`` exit code 1 means "no
    matches" and is treated as an empty result, not an error.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(clone), "grep", "-nI", "-E", _MARKER_RE],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(
            "outstanding-todo scan: git grep failed for %s (%s): %s",
            repo_id,
            clone,
            exc,
        )
        return []
    if proc.returncode not in (0, 1):
        logger.warning(
            "outstanding-todo scan: git grep on %s (%s) exited %d: %s",
            repo_id,
            clone,
            proc.returncode,
            proc.stderr.strip(),
        )
        return []

    markers: list[TodoMarker] = []
    for raw in proc.stdout.splitlines():
        # git grep -n emits ``<path>:<line>:<content>``.
        parts = raw.split(":", 2)
        if len(parts) < 3:
            continue
        path, lineno, content = parts
        try:
            line = int(lineno)
        except ValueError:
            continue
        marker = _first_marker(content)
        if marker is None:
            continue
        markers.append(
            TodoMarker(
                repo_id=repo_id,
                path=path.replace("\\", "/"),
                line=line,
                marker=marker,
                text=_trim(content),
            )
        )
    return markers


def scan_outstanding_todos(
    repo_clones: dict[str, Path],
    *,
    max_per_repo: int = MAX_PER_REPO,
    max_total: int = MAX_TOTAL,
) -> list[TodoMarker]:
    """Deterministically scan every clone for outstanding markers.

    Markers are sorted by ``(repo_id, path, line)`` *before* the
    ``max_per_repo`` / ``max_total`` caps are applied, so truncation is
    reproducible across calls. Clones that are not git repos are skipped
    (see :func:`_scan_clone`).
    """
    found: list[TodoMarker] = []
    for repo_id in sorted(repo_clones):
        found.extend(_scan_clone(repo_id, repo_clones[repo_id]))
    found.sort(key=lambda m: (m.repo_id, m.path, m.line))

    capped: list[TodoMarker] = []
    per_repo: dict[str, int] = {}
    for marker in found:
        if len(capped) >= max_total:
            break
        if per_repo.get(marker.repo_id, 0) >= max_per_repo:
            continue
        per_repo[marker.repo_id] = per_repo.get(marker.repo_id, 0) + 1
        capped.append(marker)
    return capped


def format_outstanding_todos(markers: list[TodoMarker]) -> str:
    """Render *markers* as a deterministic Markdown listing grouped by repo.

    Returns ``"(none found)"`` when *markers* is empty. When a per-repo or
    global cap truncated the results (a group reaches :data:`MAX_PER_REPO`
    or the total reaches :data:`MAX_TOTAL`), an explicit note is appended
    so silent truncation is never possible.
    """
    if not markers:
        return "(none found)"

    by_repo: dict[str, list[TodoMarker]] = {}
    for marker in markers:
        by_repo.setdefault(marker.repo_id, []).append(marker)

    lines: list[str] = []
    for repo_id in sorted(by_repo):
        group = by_repo[repo_id]
        lines.append(f"### `{repo_id}`")
        for marker in group:
            lines.append(
                f"- `{marker.path}:{marker.line}` [{marker.marker}] {marker.text}"
            )
        if len(group) >= MAX_PER_REPO:
            lines.append(
                f"  (... per-repo cap of {MAX_PER_REPO} reached for `{repo_id}`; "
                "additional markers omitted)"
            )
    if len(markers) >= MAX_TOTAL:
        lines.append(
            f"(... global cap of {MAX_TOTAL} markers reached; additional markers omitted)"
        )
    return "\n".join(lines)
