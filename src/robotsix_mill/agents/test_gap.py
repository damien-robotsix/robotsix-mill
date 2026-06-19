"""The test-gap agent: dedicated test-coverage oversight.

Identifies modules with zero dedicated test coverage, prioritizes by
complexity, I/O surface, and state-transition logic, and proposes draft
tickets for the highest-priority gaps.

Seam: tests monkeypatch ``run_test_gap_agent``. Structured output so
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
SYSTEM_PROMPT: str = load_periodic_system_prompt("test_gap")

MAX_GAPS = 5

TestGapResult = PeriodicAgentResult


def _test_gap_dynamic_kwargs(settings: Settings) -> dict[str, Any]:
    from pydantic_ai.usage import UsageLimits

    return {
        "usage_limits": UsageLimits(
            request_limit=settings.test_gap_request_limit,
            tool_calls_limit=settings.test_gap_max_tool_calls,
        ),
        "max_errors": settings.test_gap_max_errors,
    }


run_test_gap_agent = make_agent_runner(
    definition_name="test_gap",
    prompt_tail="Perform the test-gap inspection and return your result.",
    max_gaps=MAX_GAPS,
    include_forge_url=True,
    include_run_command=True,
    dynamic_kwargs_fn=_test_gap_dynamic_kwargs,
)
