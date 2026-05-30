"""Drop oldest messages from the front when estimated token count
exceeds a configurable threshold.

Only the message list is shortened — individual messages are never
mutated.  Token estimation uses the chars/4 heuristic, preferring
``msg.json()`` when available (as in pydantic-ai runtime objects).
"""

from __future__ import annotations

import json


def _estimate_tokens(msg) -> int:
    """Token estimate for a single message (chars // 4).

    Prefers ``msg.json()`` when available (pydantic-ai runtime objects
    carry it); falls back to summing part content and args for plain
    dataclass instances.
    """
    if hasattr(msg, "json"):
        return len(msg.json()) // 4
    total = 0
    for part in getattr(msg, "parts", []):
        content = getattr(part, "content", None)
        if isinstance(content, str):
            total += len(content)
        args = getattr(part, "args", None)
        if isinstance(args, str):
            total += len(args)
        elif isinstance(args, dict):
            total += len(json.dumps(args))
    return total // 4


def compress_history(
    messages: list,
    *,
    max_tokens: int = 100_000,
    keep_last: int = 5,
) -> list:
    """Drop oldest messages from the front until the remaining ones
    fit within ``max_tokens``, preserving the last ``keep_last``
    messages unconditionally.

    Returns a slice of the original list — message objects themselves
    are never modified.  Under-budget lists (and lists where
    ``max_tokens <= 0``) are returned unchanged.
    """
    # Non-positive max_tokens means "no limit" — never compress.
    if max_tokens <= 0:
        return messages

    total = sum(_estimate_tokens(m) for m in messages)
    if total <= max_tokens:
        return messages  # under budget — same list object

    n = len(messages)
    # Try dropping from the front, one message at a time, but never
    # drop into the protected tail of size *keep_last*.
    for i in range(n - keep_last):
        remaining = messages[i + 1 :]
        if sum(_estimate_tokens(m) for m in remaining) <= max_tokens:
            return remaining

    # Still over budget — fall back to just the protected tail.
    return messages[max(0, n - keep_last) :]
