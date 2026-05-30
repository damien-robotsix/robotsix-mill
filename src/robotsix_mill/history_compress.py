"""Token-aware history compression for agent message histories.

Provides ``compress_history``, a coarse char/4 heuristic that drops
oldest messages from a message history when the estimated token count
exceeds a budget, while preserving a configurable tail of recent
messages unconditionally.
"""

from __future__ import annotations


def compress_history(
    message_history: list,
    *,
    history_max_tokens: int,
    history_keep_last: int,
) -> list:
    """Drop oldest messages from *message_history* when the estimated
    token count exceeds *history_max_tokens*, preserving the last
    *history_keep_last* messages unconditionally.

    Token estimation uses a coarse char/4 heuristic (≈ English prose).
    Returns the original list when *history_max_tokens* ≤ 0 or the
    budget is already satisfied.
    """
    if not message_history or history_max_tokens <= 0:
        return message_history

    # Estimate total tokens: sum of JSON-serialised message length / 4.
    total_est = sum(len(m.json()) // 4 for m in message_history)

    if total_est <= history_max_tokens:
        return message_history

    keep_last = max(history_keep_last, 0)
    # Walk from the front, dropping messages until we're within budget
    # or only *keep_last* remain.
    for i in range(len(message_history) - keep_last):
        dropped_est = len(message_history[i].json()) // 4
        total_est -= dropped_est
        if total_est <= history_max_tokens:
            return message_history[i + 1 :]

    # Budget still exceeded even after dropping all but keep_last;
    # return only the tail.
    return message_history[-keep_last:] if keep_last else message_history[-1:]
