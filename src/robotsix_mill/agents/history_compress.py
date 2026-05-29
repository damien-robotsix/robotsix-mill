"""Elide older ToolReturnPart content when estimated token count
exceeds a configurable threshold.

Only tool outputs are touched — user messages, system prompts, and
model responses are never modified.  Token estimation uses the
chars/4 heuristic to avoid a tiktoken dependency.
"""

from __future__ import annotations


def compress_history(
    messages: list,
    *,
    max_tokens: int = 100_000,
    keep_last: int = 5,
) -> list:
    """Elide older ToolReturnPart content when estimated tokens exceed
    ``max_tokens``.

    Returns the same list (mutated in place) — callers that need the
    original should pass a copy.  Under-budget lists are returned
    unchanged.
    """
    total_chars = 0
    for m in messages:
        for part in getattr(m, "parts", []):
            content = getattr(part, "content", None)
            if content is not None and isinstance(content, str):
                total_chars += len(content)
            elif hasattr(part, "args") and isinstance(part.args, str):
                total_chars += len(part.args)
    if total_chars / 4 < max_tokens:
        return messages  # under budget, no compression needed

    from pydantic_ai.messages import ToolReturnPart

    tool_return_count = 0
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        for part in getattr(msg, "parts", []):
            if isinstance(part, ToolReturnPart):
                tool_return_count += 1
                if tool_return_count > keep_last:
                    part.content = (
                        f"[earlier output elided — "
                        f"{len(part.content)} chars]"
                    )
    return messages
