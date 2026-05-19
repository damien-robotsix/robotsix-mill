"""Text utilities — smart truncation and other string helpers."""

from __future__ import annotations


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
        (". ", 2), ("! ", 2), ("? ", 2),
        (".\n", 2), ("!\n", 2), ("?\n", 2),
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
