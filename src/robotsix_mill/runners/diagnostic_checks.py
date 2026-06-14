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

    A check exposes a ``name`` and a parameterless :meth:`run` that
    returns a :class:`DiagnosticCheckResult`. The ``run`` signature is
    deliberately parameterless for now — checks pull their own
    ``Settings()`` like the existing runners. A later ticket may widen it
    to accept a shared context if one is needed.
    """

    name: str

    def run(self) -> DiagnosticCheckResult:
        """Execute the check and return its result."""
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
