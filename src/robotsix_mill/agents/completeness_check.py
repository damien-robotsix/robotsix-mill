"""The completeness-check agent: inspects the repository for incomplete
feature wiring — missing config mappings, missing defaults, routes with
no button, runners with no CLI, and agent files with no caller — then
files draft tickets proposing completion for each discovered gap.

Seam: tests monkeypatch ``run_completeness_check_agent``. Structured
output so the runner has a clear result to work with.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from .periodic_base import (
    PeriodicAgentResult,
    load_periodic_system_prompt,
    make_agent_runner,
)

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)
SYSTEM_PROMPT: str = load_periodic_system_prompt("completeness_check")

MAX_GAPS = 12

CompletenessCheckResult = PeriodicAgentResult


def _completeness_check_dynamic_kwargs(settings: Settings) -> dict[str, Any]:
    from pydantic_ai.usage import UsageLimits

    return {
        "usage_limits": UsageLimits(
            request_limit=settings.completeness_check_request_limit
        )
    }


run_completeness_check_agent = make_agent_runner(
    definition_name="completeness_check",
    prompt_tail="Scan the repository for incomplete feature wiring and return your findings.",
    max_gaps=MAX_GAPS,
    dynamic_kwargs_fn=_completeness_check_dynamic_kwargs,
)
