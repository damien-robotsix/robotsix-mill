"""Trimming helpers for retry/audit/re-refine passes.

When a stage re-invokes an agent on the same ticket (test-failure
retry, reviewer sendback, re-refine), the agent already knows the full
context from the first pass.  Re-sending the full accumulated lifecycle
context — spec, epic context, memory ledger, reference files — inflates
every call.  This module provides helpers to trim the context down to
the delta: the specific failing item plus a minimal spec reminder.
"""

from __future__ import annotations


def trim_spec_for_retry(spec: str, *, max_chars: int = 800) -> str:
    """Return a minimal version of *spec* suitable for a retry pass.

    Keeps the first *max_chars* characters, advancing to the next
    paragraph boundary so the truncation is clean.  On a retry pass
    the agent already saw the full spec on the first pass; this
    reminder is just enough to re-orient it.
    """
    if len(spec) <= max_chars:
        return spec

    cut = spec.rfind("\n\n", 0, max_chars)
    if cut == -1:
        cut = spec.rfind("\n", 0, max_chars)
    if cut == -1:
        cut = max_chars

    omitted = len(spec) - cut
    return (
        spec[:cut] + f"\n\n[... spec truncated: {omitted} chars of detail omitted — "
        "you already read the full spec on the first pass]"
    )


def trim_draft_for_re_refine(draft: str, *, max_chars: int = 800) -> str:
    """Return a minimal version of *draft* for a refine re-refine pass.

    Keeps the first *max_chars* characters, advancing to the next
    paragraph boundary.  On a re-refine pass the agent only needs the
    reviewer's delta comments + a brief reminder of the draft's topic.
    """
    return trim_spec_for_retry(draft, max_chars=max_chars)
