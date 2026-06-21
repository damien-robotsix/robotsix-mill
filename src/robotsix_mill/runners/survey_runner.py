"""Survey runner — backward-compat stub. See periodic_runner."""

from __future__ import annotations

from ..config import RepoConfig, Settings
from .periodic_runner import (
    PERIODIC_PASS_CONFIGS,
    SurveyPassResult,
    run_periodic_pass,
)


def run_survey_pass(
    session_id: str, repo_config: RepoConfig | None = None
) -> SurveyPassResult:
    settings = Settings()
    from ..agents.web_tools import reset_trace_web_fetch_budget
    from ..agents.web_knowledge import reset_trace_web_search_budget

    reset_trace_web_fetch_budget(
        settings.survey_web_fetch_max_calls,
        settings.survey_web_fetch_max_total_bytes,
    )
    reset_trace_web_search_budget(settings.survey_web_search_max_calls)
    return run_periodic_pass(
        session_id,
        repo_config,
        config=PERIODIC_PASS_CONFIGS["survey"],
        settings=settings,
    )
