"""The mypy-baseline agent: monitors mypy baseline drift continuously.

Counts mypy-baseline.txt and mypy-baseline-test.txt entries each pass,
compares against memory-stored previous counts, triages growth by error
category, and files targeted draft tickets.

Seam: tests monkeypatch ``run_mypy_baseline_agent``. Structured output so
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
SYSTEM_PROMPT: str = load_periodic_system_prompt("mypy_baseline")

MAX_GAPS = 5

MyPyBaselineResult = PeriodicAgentResult


def _mypy_baseline_dynamic_kwargs(settings: Settings) -> dict[str, Any]:
    from pydantic_ai.usage import UsageLimits

    return {
        "usage_limits": UsageLimits(request_limit=settings.mypy_baseline_request_limit)
    }


run_mypy_baseline_agent = make_agent_runner(
    definition_name="mypy_baseline",
    prompt_tail="Count baseline entries, compare against memory, triage growth, and return your result.",
    max_gaps=MAX_GAPS,
    include_run_command=True,
    include_parallel_commands=True,
    dynamic_kwargs_fn=_mypy_baseline_dynamic_kwargs,
)
