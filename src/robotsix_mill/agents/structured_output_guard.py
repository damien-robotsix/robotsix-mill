"""Post-hoc structured-output guard shared by the implement / review /
retrospect agents.

When a structured-output agent's final message fails to parse as the
expected pydantic model, pydantic-ai's transport returns the raw text
unchanged. The downstream stage then crashes (``'str' has no attribute
'verdict'`` / ``'updated_memory'``) or blocks the ticket. The implement
stage originally inlined a "re-prompt once with a focused reminder
before degrading" guard in ``coordinating.py``; this module extracts
that block so the same recovery can be wired into the other two
structured-output agents.

The helper performs exactly ONE additional run via
:func:`robotsix_mill.agents.retry.run_agent`. Caller-side terminal
coercion (``_coerce_verdict`` / ``_coerce_result`` / the implement
stage's ``ImplementResult(summary=...)`` fallback) remains the final
safety net when the re-prompt also fails.
"""

from __future__ import annotations

import logging
from typing import Any, TypeVar


log = logging.getLogger(__name__)


T = TypeVar("T")


def reprompt_if_unstructured(
    *,
    result: Any,
    agent: Any,
    expected_type: type[T],
    reprompt_message: str,
    settings: Any,
    what: str,
    run_kwargs: dict[str, Any] | None = None,
    require_no_tool_calls: bool = False,
    char_threshold: int = 10_000,
) -> Any:
    """Re-prompt *agent* once when ``result.output`` is unstructured.

    Returns *result* unchanged when ``result.output`` is already an
    instance of *expected_type*. Otherwise inspects the raw output and,
    when the prose-only criteria are met, re-prompts *agent* once via
    :func:`robotsix_mill.agents.retry.run_agent` with the original
    message history attached, and returns the new result. On re-prompt
    exception, logs a warning and returns the ORIGINAL ``result`` so
    the caller's terminal coercion still runs.

    Detection rule:

    - ``isinstance(result.output, expected_type)`` is False, AND
    - ``len(str(result.output)) >= char_threshold``, AND
    - when *require_no_tool_calls* is True: no ``tool-call`` or
      ``tool-return`` part is present anywhere in
      ``result.all_messages()`` (the implement-specific gate).

    *run_kwargs* is forwarded to ``agent.run_sync`` on the re-prompt
    (e.g. ``{"usage_limits": limits}``); ``message_history`` is owned
    by the guard and must NOT appear in *run_kwargs*.
    """
    from .retry import run_agent

    if isinstance(result.output, expected_type):
        return result

    raw_output = str(result.output)
    if len(raw_output) < char_threshold:
        return result

    if require_no_tool_calls:
        try:
            messages = result.all_messages()
        except Exception:
            messages = []
        has_tool_call = any(
            getattr(part, "part_kind", "") in ("tool-call", "tool-return")
            for msg in messages
            for part in getattr(msg, "parts", [])
        )
        if has_tool_call:
            return result
        log.warning(
            "%s: prose-only response (%d chars) with no tool calls detected; "
            "re-prompting agent to use tools",
            what,
            len(raw_output),
        )
    else:
        log.warning(
            "%s: prose-only structured-output failure (%d chars) detected; "
            "re-prompting agent for structured output",
            what,
            len(raw_output),
        )

    extra_kwargs = dict(run_kwargs or {})
    try:
        original_messages = result.all_messages()
    except Exception:
        original_messages = []
    try:
        new_result = run_agent(
            agent,
            lambda h: h.run_sync(
                reprompt_message,
                message_history=original_messages,
                **extra_kwargs,
            ),
            settings=settings,
            what=what,
        )
    except Exception as reprompt_exc:
        log.warning(
            "%s: re-prompt after prose-only failed (%s); returning original result",
            what,
            reprompt_exc,
        )
        return result
    return new_result
