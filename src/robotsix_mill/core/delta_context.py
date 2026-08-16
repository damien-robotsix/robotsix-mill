"""Trimming helpers for retry/audit/re-refine passes.

When a stage re-invokes an agent on the same ticket (test-failure
retry, reviewer sendback, re-refine), the agent already knows the full
context from the first pass.  Re-sending the full accumulated lifecycle
context — spec, epic context, memory ledger, reference files — inflates
every call.  This module provides helpers to trim the context down to
the delta: the specific failing item plus a minimal spec reminder.
"""

from __future__ import annotations

from typing import Any

from .text_utils import tail_keep


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


def compact_message_history(
    message_history: list[Any],
    *,
    max_turns: int = 8,
    summary_max_chars: int = 3000,
) -> list[Any]:
    """Cap a pydantic-ai ``message_history`` to the last *max_turns* tool
    exchanges, replacing the dropped prefix with a short rolling summary.

    The implement resume path replays the full prior transcript, so the
    history grows with every tool turn (file dumps, git-diff output) until
    it dominates the request.  This helper keeps the most recent *max_turns*
    assistant tool-call rounds and summarises everything older.

    Only WHOLE tool turns are dropped: a ``ModelResponse`` carrying
    ``ToolCallPart``s and the immediately following ``ModelRequest``
    carrying their ``ToolReturnPart``s are always removed together, so no
    tool return is ever orphaned from its tool call (a 400 failure mode on
    the DeepSeek capable tier).  Conversations with no tool exchanges are
    returned unchanged, and the kept suffix always begins at the start of
    an assistant tool-call message so its returns follow intact.
    """
    if max_turns <= 0 or not message_history:
        return message_history

    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        ToolCallPart,
        UserPromptPart,
    )

    # A tool turn starts at a ModelResponse carrying at least one tool
    # call; its returns live in the next ModelRequest.  Counting these
    # starts from the end gives us the last-N turns to retain.
    turn_starts: list[int] = []
    for i, msg in enumerate(message_history):
        if isinstance(msg, ModelResponse) and any(
            isinstance(part, ToolCallPart) for part in msg.parts
        ):
            turn_starts.append(i)

    if len(turn_starts) <= max_turns:
        return message_history

    keep_from = turn_starts[len(turn_starts) - max_turns]
    if keep_from <= 0:
        # The first message is itself an assistant tool call (no leading
        # user prompt to drop) — cutting would orphan its returns.
        return message_history

    dropped = message_history[:keep_from]
    kept = message_history[keep_from:]
    summary = _summarize_history_prefix(dropped, summary_max_chars)
    return [ModelRequest(parts=[UserPromptPart(content=summary)]), *kept]


def _summarize_history_prefix(dropped: list[Any], max_chars: int) -> str:
    """Build a deterministic rolling summary of the dropped older turns.

    Renders one compact line per dropped part (tool call, tool result,
    assistant/user text) and keeps the most-recent tail within
    *max_chars* so the summary stays bounded while retaining the newest
    dropped exchanges.
    """
    from pydantic_ai.messages import ModelRequest, ModelResponse

    lines: list[str] = []
    for msg in dropped:
        if isinstance(msg, ModelResponse):
            render = _render_assistant_part
        elif isinstance(msg, ModelRequest):
            render = _render_user_part
        else:
            continue
        for part in msg.parts:
            line = render(part)
            if line:
                lines.append(line)

    body = "\n".join(lines) if lines else "(no earlier tool exchanges)"
    preamble = (
        "Earlier turns of this conversation have been summarized and dropped. "
        "Do not reference tool_call_ids from the dropped prefix.\n\n"
    )
    budget = max(0, max_chars - len(preamble))
    if budget <= 0:
        return preamble
    return preamble + tail_keep(body, budget, label="earlier turns")


def _render_assistant_part(part: Any) -> str | None:
    """Render one assistant-message part as a summary line (or ``None``)."""
    from pydantic_ai.messages import TextPart, ToolCallPart

    if isinstance(part, ToolCallPart):
        return f"- tool call {part.tool_name}({_compact_tool_args(part.args)})"
    if isinstance(part, TextPart):
        return f"- assistant: {_clip_text(part.content)}"
    return None


def _render_user_part(part: Any) -> str | None:
    """Render one user-message part as a summary line (or ``None``)."""
    from pydantic_ai.messages import TextPart, ToolReturnPart, UserPromptPart

    if isinstance(part, ToolReturnPart):
        return f"- tool result ({part.tool_name}): {_clip_text(part.content)}"
    if isinstance(part, UserPromptPart):
        return "- user prompt (spec/memory) omitted — re-sent fresh"
    if isinstance(part, TextPart):
        return f"- user: {_clip_text(part.content)}"
    return None


def _clip_text(value: Any, max_chars: int = 200) -> str:
    """Coerce a part payload to a compact single-line string."""
    if isinstance(value, str):
        text = value
    elif isinstance(value, list):
        text = "\n".join(getattr(p, "text", None) or str(p) for p in value)
    else:
        text = str(value)
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


def _compact_tool_args(args: Any, max_chars: int = 120) -> str:
    """Render a tool-call args dict as a short ``k=v`` string."""
    if not isinstance(args, dict):
        return _clip_text(args, max_chars)
    joined = ", ".join(f"{k}={_clip_text(v, 60)}" for k, v in args.items())
    if len(joined) <= max_chars:
        return joined
    return joined[:max_chars] + "…"
