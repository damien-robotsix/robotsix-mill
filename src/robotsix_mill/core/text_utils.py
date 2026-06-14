"""Text utilities — smart truncation and other string helpers."""

from __future__ import annotations

import html
import re

# Tags whose entire content (open → close, inclusive) must be
# dropped before tag stripping. Script and style payloads are
# never useful for an LLM and they're often the bulk of a docs
# page's byte budget.
_BLOCK_TAGS = ("script", "style", "noscript", "svg")


def _strip_block_tag(body: str, tag: str) -> str:
    """Remove every ``<tag>...</tag>`` block (including content) from
    *body*. Case-insensitive; tolerant of attributes on the open tag.

    Implemented with a non-greedy regex rather than a real HTML
    parser — wrong for adversarial input, fine for the docs pages
    we routinely fetch. The LLM is the eventual consumer, not a
    browser, so layout fidelity doesn't matter."""
    pattern = re.compile(
        rf"<{tag}\b[^>]*>.*?</{tag}\s*>",
        re.IGNORECASE | re.DOTALL,
    )
    return pattern.sub(" ", body)


def html_to_text(body: str) -> str:
    """Strip *body* of HTML markup, return whitespace-collapsed text.

    - drops ``<script>``, ``<style>``, ``<noscript>``, ``<svg>``
      blocks entirely (content + tags);
    - removes all remaining tags;
    - unescapes HTML entities (``&amp;`` → ``&``, ``&nbsp;`` → space);
    - collapses runs of whitespace to a single space, then squashes
      multiple blank lines back to one (preserve paragraph breaks).

    Dependency-free — regex + ``html.unescape``. Adequate for the
    docs pages mill fetches; a malicious page could trick this into
    leaking script content as text, but the agent's context is the
    only consumer and a script-tag dump is just noise to the LLM.
    """
    for tag in _BLOCK_TAGS:
        body = _strip_block_tag(body, tag)
    # Strip remaining tags.
    body = re.sub(r"<[^>]+>", " ", body)
    # Unescape entities.
    body = html.unescape(body)
    # Collapse whitespace: runs of horizontal whitespace → one space,
    # but keep newlines so the LLM still sees paragraph structure.
    body = re.sub(r"[ \t]+", " ", body)
    # Collapse 3+ consecutive newlines (created by the tag stripping)
    # to exactly two.
    body = re.sub(r"\n{3,}", "\n\n", body)
    # Strip leading/trailing whitespace on each line.
    body = "\n".join(line.strip() for line in body.split("\n"))
    # And drop pure-empty surrounding lines.
    return body.strip()


def truncate_at_boundary(text: str, max_chars: int) -> str:
    """Truncate *text* at the last strong boundary before *max_chars*.

    If ``len(text) <= max_chars`` the string is returned unchanged.
    Otherwise the function scans ``text[:max_chars]`` for the **last**
    occurrence of any of these boundaries:

    - sentence-ending punctuation followed by whitespace: ``. `` ``! `` ``? ``
    - sentence-ending punctuation at end of line: ``.\\n`` ``!\\n`` ``?\\n``
    - paragraph break: ``\\n\\n``
    - Markdown code-fence close: `` ``` ``

    Truncation happens *after* the boundary (the boundary itself is kept).
    If no boundary is found the function falls back to a hard cut at
    *max_chars*.  When truncation *does* occur a note like
    ``\\n\\n[... description truncated; N chars omitted]`` is appended.

    Returns:
        The (possibly truncated) string.
    """
    if max_chars >= len(text):
        return text

    prefix = text[:max_chars]

    # Each entry is (literal pattern, length of the pattern).
    # We search for the **last** occurrence of each pattern in *prefix*;
    # the truncation point is *after* the pattern (pos + length).
    boundaries: list[tuple[str, int]] = [
        (". ", 2),
        ("! ", 2),
        ("? ", 2),
        (".\n", 2),
        ("!\n", 2),
        ("?\n", 2),
        ("\n\n", 2),
        ("```", 3),
    ]

    best: int = -1
    for pat, length in boundaries:
        pos = prefix.rfind(pat)
        if pos != -1:
            best = max(best, pos + length)

    if best == -1:
        # No natural boundary — fall back to hard truncation.
        best = max_chars

    truncated = text[:best].rstrip()
    omitted = len(text) - len(truncated)
    return f"{truncated}\n\n[... description truncated; {omitted} chars omitted]"


def tail_keep(text: str, max_chars: int, *, label: str = "content") -> str:
    """Keep the most-recent tail of *text*, dropping the oldest content.

    If ``len(text) <= max_chars`` the string is returned unchanged.
    Otherwise the last *max_chars* characters are kept, advanced forward
    to the next newline boundary so the first kept line is complete, and
    a ``[... <label> truncated: N chars omitted]`` note is prepended.

    This is the correct primitive for chronological logs / append-only
    ledgers where the newest content matters most — unlike
    :func:`truncate_at_boundary`, which keeps the HEAD.

    Returns:
        The (possibly tail-truncated) string.
    """
    if max_chars >= len(text):
        return text

    original_size = len(text)
    # Find the cut point (keep the last max_chars), then advance to the
    # next newline so the first kept line is a complete line.
    cut_point = original_size - max_chars
    nl_idx = text.find("\n", cut_point)
    if nl_idx != -1:
        kept = text[nl_idx + 1 :]  # start after the newline
    else:
        kept = text[cut_point:]  # fallback (no newline found)
    omitted = original_size - len(kept)
    return f"[... {label} truncated: {omitted} chars omitted]\n\n{kept}"


def head_tail_keep(text: str, max_chars: int, *, label: str = "content") -> str:
    """Keep a head slice and a tail slice of *text*, dropping the middle.

    If ``max_chars == 0`` or ``len(text) <= max_chars`` the string is
    returned unchanged. Otherwise the budget is split ~60% head / 40%
    tail; each slice is advanced to a line boundary so kept lines are
    complete, and the two are joined by an explicit marker line
    ``[... <label> truncated: N chars omitted from the middle ...]``.

    Head+tail (not pure head or tail) is the correct primitive for
    git diffs: both the early and late files in the diff get
    representation, unlike :func:`tail_keep` (keeps only the end) or
    :func:`truncate_at_boundary` (keeps only the start).

    Returns:
        The (possibly middle-truncated) string.
    """
    if max_chars == 0 or len(text) <= max_chars:
        return text

    head_budget = (max_chars * 6) // 10
    tail_budget = max_chars - head_budget

    # Head slice: cut at or before head_budget, retreat to the last
    # newline so the last kept line is complete.
    head_cut = head_budget
    nl_idx = text.rfind("\n", 0, head_cut)
    head = text[: nl_idx + 1] if nl_idx != -1 else text[:head_cut]

    # Tail slice: keep the last tail_budget chars, advance to the next
    # newline so the first kept line is complete.
    tail_start = len(text) - tail_budget
    nl_idx = text.find("\n", tail_start)
    tail = text[nl_idx + 1 :] if nl_idx != -1 else text[tail_start:]

    omitted = len(text) - len(head) - len(tail)
    return (
        f"{head}"
        f"\n\n[... {label} truncated: {omitted} chars omitted from the middle ...]\n\n"
        f"{tail}"
    )
