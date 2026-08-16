"""Shared git-diff parsing helpers for agent context assembly.

``changed_line_ranges_from_diff`` is the single source of truth for
turning a unified ``git diff`` into per-file changed-region line ranges,
which ``build_preseed_history`` uses to excerpt-preload only the parts
of a file an agent actually needs.  Originally introduced for the review
stage (PR #2902); the implement stage now reuses it on retry passes where
the prior attempt's edits are already on disk.
"""

from __future__ import annotations

import re


def _split_diff_by_file(diff: str) -> list[tuple[str, str]]:
    """Split a unified git diff into per-file ``(path, chunk_text)`` pairs.

    Splits on ``^diff --git `` boundaries and extracts the file path from
    the ``+++ b/<path>`` line in each chunk.  Empty chunks and deletion-only
    chunks (``+++ /dev/null``) are skipped.  Pairs are returned in the same
    order as the original diff.
    """
    chunks = re.split(r"(?=^diff --git )", diff, flags=re.MULTILINE)
    result: list[tuple[str, str]] = []
    for chunk in chunks:
        stripped = chunk.strip()
        if not stripped:
            continue
        path_match = re.search(r"^\+\+\+ b/(.+)$", chunk, re.MULTILINE)
        if not path_match:
            continue
        path = path_match.group(1)
        if path == "/dev/null":
            # Deletion-only chunk — no file to review
            continue
        result.append((path, stripped))
    return result


_HUNK_HEADER_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@",
    re.MULTILINE,
)


def changed_line_ranges_from_diff(diff: str) -> dict[str, list[tuple[int, int]]]:
    """Extract per-file changed line ranges from a unified diff.

    Returns ``{path: [(start, end), …]}`` with 1-indexed inclusive
    ranges taken from each hunk's new-side ``@@ -a,b +c,d @@`` header.
    Only genuinely MODIFIED files are included — a new file (old side
    ``-0,0``) already has its entire content in the diff, and a deletion
    (new side ``+0,0``) has no content left to excerpt, so both are
    skipped. Paths map to an empty list never appear.
    """
    ranges: dict[str, list[tuple[int, int]]] = {}
    for path, chunk_text in _split_diff_by_file(diff):
        for m in _HUNK_HEADER_RE.finditer(chunk_text):
            old_count = int(m.group("old_count") or 1)
            new_count = int(m.group("new_count") or 1)
            if old_count == 0 or new_count == 0:
                continue
            new_start = int(m.group("new_start"))
            ranges.setdefault(path, []).append((new_start, new_start + new_count - 1))
    return ranges
