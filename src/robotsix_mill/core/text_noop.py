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
    "no notable issue",
    "no issues",
    "no issue",
    "clean run",
    "nothing to flag",
    "nothing to report",
    "no improvement",
    "no action needed",
    "no concerns",
    "no notable finding",
    "all good",
    "no changes needed",
    "clean ticket",
    "nothing notable",
)


# Lower-case substrings that mark a title as a completion-announcement
# no-op — the agent is declaring it's finished, not filing an issue.
COMPLETION_ANNOUNCEMENT_MARKERS: tuple[str, ...] = (
    "refine complete",
    "spec produced",
    "refinement complete",
)


def is_completion_announcement(title: str | None) -> bool:
    """True if *title* is a completion-announcement no-op."""
    t = (title or "").strip().lower()
    if not t:
        return False
    return any(m in t for m in COMPLETION_ANNOUNCEMENT_MARKERS)


def is_noop_report(title: str | None) -> bool:
    """True if *title* is an empty or 'nothing to report' no-op."""
    t = (title or "").strip().lower()
    if not t:
        return True
    return any(m in t for m in NOOP_MARKERS)


# Lower-case substrings that mark a BODY as a placeholder pointer rather
# than real content. Lives here rather than in the refine stage because
# two very different callers need the same answer: refine (is this spec
# usable?) and the ticket-spawning agents (is this body worth filing?).
PLACEHOLDER_BODY_PHRASES: tuple[str, ...] = (
    "see spec above",
    "see the spec above",
    "see above",
    "see spec",
    "see the spec",
    "see description",
    "see the description",
    "spec above",
    "as above",
    "as written above",
    "see previous",
    "see below",
    "refer to spec",
    "tbd",
    "todo",
)


def is_degenerate_body(text: str | None) -> bool:
    """True when *text* is empty or a placeholder pointer, not real content.

    Only short (≤120-char) single-idea strings can match; a genuine body is
    much longer, so real content is never dropped. Punctuation-only strings
    normalise to empty and count as degenerate — a retrospect draft whose
    body was literally ``"..."`` reached the board and then blocked in
    refine, which is what this guards.
    """
    import re

    stripped = (text or "").strip()
    if not stripped:
        return True
    if len(stripped) > 120:
        return False
    norm = " ".join(re.sub(r"[^a-z0-9 ]+", " ", stripped.lower()).split())
    if not norm:
        return True
    return any(p in norm for p in PLACEHOLDER_BODY_PHRASES)
