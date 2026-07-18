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


# ── zero-tool-call detection helpers ──────────────────────────────────────


def _has_tool_calls(result: Any) -> bool:
    """Return True when any ``tool-call`` or ``tool-return`` part exists
    in *result*'s message history."""
    try:
        messages = result.all_messages()
    except Exception:
        return False
    return any(
        getattr(part, "part_kind", "") in ("tool-call", "tool-return")
        for msg in messages
        for part in getattr(msg, "parts", [])
    )


def _zero_tool_call_reprompt_needed(
    result: Any,
    expected_type: type,
    what: str,
) -> bool:
    """Return True when the agent produced zero tool calls AND this is
    not a legitimate no-change pass.

    A structured output with ``no_change_needed=True`` is a deliberate
    signal that the spec is already satisfied — the agent correctly
    made zero edits.  Everything else with zero tool calls needs a
    re-prompt.
    """
    if _has_tool_calls(result):
        return False

    # Carve-out: structured output that explicitly declares no change
    # needed (ticket's intent is already satisfied by the codebase).
    if isinstance(result.output, expected_type):
        if getattr(result.output, "no_change_needed", False):
            return False

    log.warning(
        "%s: zero tool calls detected (%d chars of output); "
        "re-prompting agent to use tools",
        what,
        len(str(result.output)),
    )
    return True


# ── main guard ────────────────────────────────────────────────────────────


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
    """Re-prompt *agent* once when ``result.output`` is unstructured,
    OR when *require_no_tool_calls* is True and the pass produced zero
    tool calls.

    The zero-tool-call gate runs BEFORE the structured-output check so
    that a structured ``ImplementResult`` the model produced without
    ever calling a tool does not slip through — that is the exact
    zero-tool-call pass this guard exists to catch.

    Returns *result* unchanged when:

    - *require_no_tool_calls* is True, zero tool calls were made, BUT
      the output is a structured result with ``no_change_needed=True``
      (a deliberate signal the spec is already satisfied); OR
    - ``result.output`` is already an instance of *expected_type* (and
      the zero-tool-call gate above did not fire); OR
    - ``len(str(result.output)) < char_threshold`` (too short to act
      on); OR
    - the re-prompt itself raises (original result returned so the
      caller's terminal coercion still runs).

    Detection rule:

    - Zero-tool-call gate (when *require_no_tool_calls* is True):
      no ``tool-call`` or ``tool-return`` part present AND output is
      NOT a structured ``no_change_needed=True`` pass → re-prompt.
    - Structured-output gate:
      ``isinstance(result.output, expected_type)`` is False, AND
      ``len(str(result.output)) >= char_threshold``, AND
      (when *require_no_tool_calls* is True) tool calls ARE present
      (the zero-tool-call gate above would have caught a no-tool-call
      case) → re-prompt.

    *run_kwargs* is forwarded to ``agent.run_sync`` on the re-prompt
    (e.g. ``{"usage_limits": limits}``); ``message_history`` is owned
    by the guard and must NOT appear in *run_kwargs*.
    """
    from .retry import run_agent

    # ── Zero-tool-call gate (BEFORE structured-output check) ──────────
    # Must run first because ``isinstance(result.output, expected_type)``
    # would return early for a structured ``ImplementResult`` that the
    # model produced without ever calling a tool — the exact zero-tool-call
    # pass this guard exists to catch.
    if require_no_tool_calls and _zero_tool_call_reprompt_needed(
        result, expected_type, what
    ):
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
                what=what,
            )
        except Exception as reprompt_exc:
            log.warning(
                "%s: re-prompt after zero-tool-call failed (%s); "
                "returning original result",
                what,
                reprompt_exc,
            )
            return result
        return new_result

    # ── Structured-output check ───────────────────────────────────────
    if isinstance(result.output, expected_type):
        return result

    # ── Short prose → no re-prompt ────────────────────────────────────
    raw_output = str(result.output)
    if len(raw_output) < char_threshold:
        return result

    # ── Unstructured path ────────────────────────────────────────────
    # When require_no_tool_calls is True and we reach here, tool calls
    # ARE present (the zero-tool-call gate above would have caught a
    # no-tool-call case).  Return early — the model used tools, so the
    # prose-only output is acceptable.
    if require_no_tool_calls:
        return result

    # require_no_tool_calls=False (review / retrospect): re-prompt for
    # structured output.
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
