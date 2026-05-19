"""Shared 'nothing to report' detection — single source of truth.

Used by BOTH the retrospect stage (its draft-spawn guard) and the
generic ``report_issue`` tool (its self-report guard) so the two can
never drift. Title-only by design: a genuine improvement/issue title
never contains these phrases, while legitimately terse real tickets
must NOT be filtered by length heuristics.
"""

from __future__ import annotations

# Lower-case substrings that mark a report as a non-actionable
# "everything is fine" no-op rather than a real improvement/issue.
NOOP_MARKERS: tuple[str, ...] = (
    "no notable issue", "no issues", "no issue", "clean run",
    "nothing to flag", "nothing to report", "no improvement",
    "no action needed", "no concerns", "no notable finding",
    "all good", "no changes needed", "clean ticket", "nothing notable",
)


def is_noop_report(title: str | None) -> bool:
    """True if *title* is an empty or 'nothing to report' no-op."""
    t = (title or "").strip().lower()
    if not t:
        return True
    return any(m in t for m in NOOP_MARKERS)
