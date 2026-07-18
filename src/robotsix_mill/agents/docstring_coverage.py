"""The docstring-coverage agent: public-API documentation oversight.

Scans Python source modules for public functions, classes, and methods
that lack docstrings, prioritizes by complexity (body length, parameter
count), and proposes draft tickets for the highest-priority gaps.

Seam: tests monkeypatch ``run_docstring_coverage_agent``. Structured
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
SYSTEM_PROMPT: str = load_periodic_system_prompt("docstring_coverage")

MAX_GAPS = 5

DocstringCoverageResult = PeriodicAgentResult


def _docstring_coverage_dynamic_kwargs(settings: Settings) -> dict[str, Any]:
    from pydantic_ai.usage import UsageLimits

    return {
        "usage_limits": UsageLimits(
            request_limit=settings.docstring_coverage_request_limit,
            tool_calls_limit=settings.docstring_coverage_max_tool_calls,
        ),
        "max_errors": settings.docstring_coverage_max_errors,
    }


run_docstring_coverage_agent = make_agent_runner(
    definition_name="docstring_coverage",
    prompt_tail="Perform the docstring-coverage inspection and return your result.",
    max_gaps=MAX_GAPS,
    include_forge_url=True,
    include_run_command=True,
    dynamic_kwargs_fn=_docstring_coverage_dynamic_kwargs,
)
