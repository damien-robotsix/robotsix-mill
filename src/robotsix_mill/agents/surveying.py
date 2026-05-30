"""The survey agent: discovers and learns from similar open-source
projects, proposing concrete improvements for the current repo.

Seam: tests monkeypatch ``run_survey_agent``. Structured output so
the runner has a clear result to work with.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..config import Settings

# One subject per run → one proposal max. The previous limit of 5
# encouraged the agent to sweep the codebase for five gaps in a
# single ~$1 run that routinely blew the 12-request budget. The
# rewritten prompt (agent_definitions/periodic/survey.yaml) caps
# the agent at one focused subject per run; this code-side cap is
# defence in depth.
MAX_GAPS = 1


class SurveyResult(BaseModel):
    updated_memory: str = ""
    draft_titles: list[str] = Field(default_factory=list)
    draft_bodies: list[str] = Field(default_factory=list)
    gap_ids: list[str] = Field(default_factory=list)


def run_survey_agent(
    *,
    settings: Settings,
    memory: str = "",
    recent_proposals: str = "",
    repo_dir=None,
) -> SurveyResult:
    """Run the survey pass.

    Discovers similar open-source projects via ``web_research``,
    researches them via ``web_research``, studies their
    approaches, and returns a structured ``SurveyResult`` with draft
    tickets for concrete improvements.

    When ``repo_dir`` is provided, the agent gets filesystem tools
    (``read_file``, ``list_dir``) and the ``explore`` scout tool so
    it can inspect the local codebase.  Without ``repo_dir`` the
    agent runs in a read-only reasoning mode (no repo access).

    The agent is constructed via
    :func:`~.periodic_base.run_periodic_agent` with the role-specific
    ``SYSTEM_PROMPT``, structured output type
    ``PromptedOutput(SurveyResult)``, ``web=True`` (for
    ``web_research``), ``report_issue=False``, and
    ``model_name=settings.survey_model``.

    Execution is wrapped in :func:`~.retry.call_with_retry`.

    Args:
        settings: Application configuration — model names, retry
            parameters, forge URL, and tool paths.
        memory: The agent's memory ledger as a Markdown string.
            Defaults to ``""`` (the agent starts a fresh ledger).
        repo_dir: Optional path to the local repository clone.
            When not ``None``, enables the ``explore``,
            ``read_file``, and ``list_dir`` tools.

    Returns:
        A ``SurveyResult`` with draft titles, bodies, and gap IDs
        clipped to ``MAX_GAPS`` (5) entries, plus the updated memory
        ledger.
    """
    from pydantic_ai.usage import UsageLimits

    from .periodic_base import run_periodic_agent

    limits = UsageLimits(request_limit=settings.survey_request_limit)
    return run_periodic_agent(
        settings=settings,
        definition_name="survey",
        model_setting=settings.survey_model,
        max_gaps=MAX_GAPS,
        repo_dir=repo_dir,
        memory=memory,
        recent_proposals=recent_proposals,
        prompt_tail="Survey similar open-source projects and return your proposals.",
        include_forge_url=True,
        usage_limits=limits,
    )
