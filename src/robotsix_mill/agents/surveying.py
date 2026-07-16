"""The survey agent: discovers and learns from similar open-source
projects, proposing concrete improvements for the current repo.

Seam: tests monkeypatch ``run_survey_agent``. Structured output so
the runner has a clear result to work with.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from .periodic_base import (
    PeriodicAgentResult,
    make_agent_runner,
)

# One subject per run → one proposal max.  The previous limit of 5
# encouraged the agent to sweep the codebase for five gaps in a
# single ~$1 run that routinely blew the 12-request budget.  The
# rewritten prompt (agent_definitions/periodic/survey.yaml) caps
# the agent at one focused subject per run; this code-side cap is
# defence in depth.
MAX_GAPS = 1

SurveyResult = PeriodicAgentResult


def _survey_dynamic_kwargs(settings: Settings) -> dict[str, Any]:
    from pydantic_ai.usage import UsageLimits

    return {"usage_limits": UsageLimits(request_limit=settings.survey_request_limit)}


run_survey_agent = make_agent_runner(
    definition_name="survey",
    prompt_tail="Survey similar open-source projects and return your proposals.",
    max_gaps=MAX_GAPS,
    include_forge_url=True,
    dynamic_kwargs_fn=_survey_dynamic_kwargs,
    fallback_level=3,
)
