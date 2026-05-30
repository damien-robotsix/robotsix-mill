"""Token-aware history compression.

Only elides ``ToolReturnPart.content`` strings — never touches user
messages, system prompts, or model responses. When the estimated token
count exceeds *max_tokens*, the oldest tool-return content strings are
truncated (their text replaced with a short placeholder) until the
budget is satisfied, while always preserving the last *keep_last*
messages unconditionally.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def compress_history(
    message_history: list,
    *,
    max_tokens: int,
    keep_last: int,
) -> list:
    """Elide ``ToolReturnPart.content`` strings from the front when the
    estimated token count exceeds *max_tokens*, preserving the last
    *keep_last* messages unconditionally.

    Token estimation uses a coarse char/4 heuristic (≈ English prose).
    Returns the original list when *max_tokens* ≤ 0 or the budget is
    already satisfied.
    """
    if not message_history or max_tokens <= 0:
        return message_history

    # Estimate total tokens: sum of JSON-serialised message length / 4.
    total_est = sum(len(m.json()) // 4 for m in message_history)

    if total_est <= max_tokens:
        return message_history

    keep_last_val = max(keep_last, 0)
    # Walk from the front, dropping messages until we're within budget
    # or only *keep_last* remain.
    for i in range(len(message_history) - keep_last_val):
        dropped_est = len(message_history[i].json()) // 4
        total_est -= dropped_est
        if total_est <= max_tokens:
            return message_history[i + 1 :]

    # Budget still exceeded even after dropping all but keep_last;
    # return only the tail.
    return message_history[-keep_last_val:] if keep_last_val else message_history[-1:]
