"""The bc-check agent: scans the repository for backward-compatibility
shims, no-op compat entry points, legacy property accessors, alias
assignments, default-arg compat branches, and legacy shape fallbacks —
then files draft tickets proposing cleanup for those that are ripe for
removal.

Seam: tests monkeypatch ``run_bc_check_agent``. Structured output so the
runner has a clear result to work with.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..config import Settings
from .periodic_base import (
    PeriodicAgentResult,
    load_periodic_system_prompt,
    make_agent_runner,
)

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)
SYSTEM_PROMPT: str = load_periodic_system_prompt("bc_check")

MAX_GAPS = 12


class BcCheckCoverage(BaseModel):
    """What the scan actually reached, so a 0-draft run is verifiable."""

    scanned_roots: list[str] = Field(
        default_factory=list,
        description="Repo-relative roots that were searched successfully (e.g. '.').",
    )
    unreachable_roots: list[str] = Field(
        default_factory=list,
        description=(
            "Planned search roots that failed (tool error, path outside the "
            "repository). Non-empty means the scan was INCOMPLETE."
        ),
    )
    search_method: str = Field(
        default="",
        description="How the patterns were searched (e.g. 'explore AST/grep over .').",
    )


class BcCheckResult(PeriodicAgentResult):
    """``PeriodicAgentResult`` plus a ``coverage`` report of the scan."""

    coverage: BcCheckCoverage = Field(
        default_factory=BcCheckCoverage,
        description=(
            "ALWAYS fill: scanned_roots, unreachable_roots, search_method. "
            "List every planned root that failed under unreachable_roots."
        ),
    )


def _bc_check_dynamic_kwargs(settings: Settings) -> dict[str, Any]:
    from pydantic_ai.usage import UsageLimits

    return {"usage_limits": UsageLimits(request_limit=settings.bc_check_request_limit)}


run_bc_check_agent = make_agent_runner(
    definition_name="bc_check",
    prompt_tail="Scan the repository for backward-compatibility code and return your findings.",
    max_gaps=MAX_GAPS,
    dynamic_kwargs_fn=_bc_check_dynamic_kwargs,
)
