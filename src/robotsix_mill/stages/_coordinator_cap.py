"""Effective coordinator tool-call cap: default, per-ticket override, backstop.

The coordinator tool-call budget (``coordinator_max_tool_calls``, default
300) is a global runaway-loop backstop shared by the implement and ci_fix
coordinator passes.  Most tickets converge well under it, but a genuinely
large ticket can need more than 300 tool calls in one pass — forcing it
into a resume→cap→block loop even when checkpoint/resume is wired.

A ticket may therefore carry a per-ticket override
(``Ticket.coordinator_max_tool_calls_override``) that raises (or lowers)
its effective cap.  The override is **clamped** to the global hard
backstop (``coordinator_max_tool_calls_hard_cap``) so an accidental
sky-high value can never disable the runaway-loop guard: the effective cap
is ``min(override, hard_cap)`` (and never below 1).  When no override is
set the effective cap is unchanged — the default ``coordinator_max_tool_calls``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..core.models import Ticket

log = logging.getLogger(__name__)

# Fallback defaults mirror the Settings field defaults so the helper stays
# correct even when handed a bare/legacy settings object missing the field.
_DEFAULT_MAX_TOOL_CALLS = 300
_DEFAULT_HARD_CAP = 1000


def effective_coordinator_max_tool_calls(settings: Any, ticket: Ticket) -> int:
    """Return the effective coordinator tool-call cap for *ticket*.

    Without a per-ticket override this is ``coordinator_max_tool_calls``.
    With an override it is ``min(override, coordinator_max_tool_calls_hard_cap)``
    (clamped to at least 1) so the global hard backstop always holds and a
    looping agent with an override still gets paused.
    """
    default = int(
        getattr(settings, "coordinator_max_tool_calls", _DEFAULT_MAX_TOOL_CALLS)
    )
    hard_cap = int(
        getattr(settings, "coordinator_max_tool_calls_hard_cap", _DEFAULT_HARD_CAP)
    )
    override = getattr(ticket, "coordinator_max_tool_calls_override", None)
    if override is None:
        return default
    effective = max(1, min(int(override), hard_cap))
    if effective != default:
        log.info(
            "%s: coordinator tool-call cap overridden to %d "
            "(requested override=%s, default=%d, hard_cap=%d)",
            getattr(ticket, "id", "?"),
            effective,
            override,
            default,
            hard_cap,
        )
    return effective
