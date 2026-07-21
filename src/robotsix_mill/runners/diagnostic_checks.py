"""Pluggable check registry for the daily diagnostic agent.

The diagnostic agent is a deterministic orchestrator (see
``diagnostic_runner``) that iterates a registry of independent checks.
This module is the seam later epic children use to add checks WITHOUT
editing the runner: a check registers itself via :func:`register_check`
and the runner picks it up through :func:`get_registered_checks`.

This skeleton ships ZERO checks — ``DIAGNOSTIC_CHECKS`` starts empty and
later tickets populate it (error detection, draft-count validation, …).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..config import Settings


@dataclass
class DiagnosticCheckContext:
    """Per-repo context the runner passes to each check on every pass."""

    board_id: str
    settings: Settings


@dataclass
class DiagnosticCheckResult:
    """Outcome of a single diagnostic check.

    Attributes:
        name: The check's identifier (mirrors ``DiagnosticCheck.name``).
        ok: Whether the check passed (no problem detected / ran cleanly).
        summary: Concise human-readable account of the outcome.
        drafts_created: Tickets filed by the check (each a ``{"id", ...}``
            dict). Defaults to empty; checks that file tickets in later
            tickets report them here so the runner can aggregate.
    """

    name: str
    ok: bool
    summary: str
    drafts_created: list[dict[str, Any]] = field(default_factory=list)


@runtime_checkable
class DiagnosticCheck(Protocol):
    """Protocol every diagnostic check satisfies.

    A check exposes a ``name`` and a :meth:`run` that takes a
    :class:`DiagnosticCheckContext` and returns a
    :class:`DiagnosticCheckResult`. The runner builds one context per
    monitored repo and invokes every registered check with it, so a
    check reads ``ctx.board_id`` / ``ctx.settings`` rather than pulling
    its own ``Settings()`` or the singular target board.
    """

    name: str

    def run(self, ctx: DiagnosticCheckContext) -> DiagnosticCheckResult:
        """Execute the check for *ctx* and return its result."""
        ...


# Module-level registry. Later epic children append checks here (directly
# or via the ``register_check`` helper). The runner reads it through
# ``get_registered_checks`` — it never needs editing to add a check.
DIAGNOSTIC_CHECKS: list[DiagnosticCheck] = []


def register_check(check: DiagnosticCheck) -> DiagnosticCheck:
    """Append *check* to the registry and return it.

    Usable as a decorator on a check instance/class or as a plain call.
    Returns the check so decorator usage preserves the bound name.
    """
    DIAGNOSTIC_CHECKS.append(check)
    return check


def get_registered_checks() -> list[DiagnosticCheck]:
    """Return a copy of the current check registry (used by the runner)."""
    return list(DIAGNOSTIC_CHECKS)


# Concrete checks (diagnostic_check_errors, diagnostic_check_recurring,
# …) self-register via ``register_check`` at their own module level.
# The runner (diagnostic_runner) imports them for their side-effect so
# that ``get_registered_checks()`` sees the full registry without
# creating a circular import back into this module.
