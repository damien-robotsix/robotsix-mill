"""The module-size agent: oversized-file oversight.

Scans Python source and test files for excessive line counts, estimates
distinct responsibilities, and proposes concrete split tickets for the
worst offenders.

Seam: tests monkeypatch ``run_module_size_agent``. Structured output so
the runner has a clear result to work with.
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
SYSTEM_PROMPT: str = load_periodic_system_prompt("module_size")

MAX_GAPS = 3

ModuleSizeResult = PeriodicAgentResult


def _module_size_dynamic_kwargs(settings: Settings) -> dict[str, Any]:
    from pydantic_ai.usage import UsageLimits

    return {
        "usage_limits": UsageLimits(
            request_limit=settings.module_size_request_limit,
            tool_calls_limit=settings.module_size_max_tool_calls,
        ),
        "max_errors": settings.module_size_max_errors,
    }


run_module_size_agent = make_agent_runner(
    definition_name="module_size",
    prompt_tail="Perform the module-size inspection and return your result.",
    max_gaps=MAX_GAPS,
    include_forge_url=True,
    include_run_command=True,
    dynamic_kwargs_fn=_module_size_dynamic_kwargs,
)
