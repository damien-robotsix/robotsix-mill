"""The survey agent: discovers and learns from similar open-source
projects, proposing concrete improvements for the current repo.

Seam: tests monkeypatch ``run_survey_agent``. Structured output so
the runner has a clear result to work with.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from ..config import Settings

MAX_GAPS = 5


class SurveyResult(BaseModel):
    updated_memory: str = ""
    draft_titles: list[str] = Field(default_factory=list)
    draft_bodies: list[str] = Field(default_factory=list)
    gap_ids: list[str] = Field(default_factory=list)


def run_survey_agent(
    *,
    settings: Settings,
    memory: str = "",
    repo_dir=None,
) -> SurveyResult:
    """Run the survey pass.

    Discovers similar open-source projects via ``web_research``,
    fetches their key files via ``web_fetch``, studies their
    approaches, and returns a structured ``SurveyResult`` with draft
    tickets for concrete improvements.

    When ``repo_dir`` is provided, the agent gets filesystem tools
    (``read_file``, ``list_dir``) and the ``explore`` scout tool so
    it can inspect the local codebase.  Without ``repo_dir`` the
    agent runs in a read-only reasoning mode (no repo access).

    The agent is constructed via :func:`~.base.build_agent` with the
    role-specific ``SYSTEM_PROMPT``, structured output type
    ``PromptedOutput(SurveyResult)``, ``web=True`` (for
    ``web_research`` and ``web_fetch``), ``report_issue=False``, and
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
    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent / "agent_definitions" / "survey.yaml"
    )

    tools: list = []
    if repo_dir is not None:
        from .explore import make_explore_tool
        from .fs_tools import build_fs_tools

        ro = [
            t for t in build_fs_tools(repo_dir, settings)
            if t.__name__ in ("read_file", "list_dir")
        ]
        tools = [make_explore_tool(settings, repo_dir), *ro]

    agent = build_agent_from_definition(
        settings, definition, tools=tools,
        model_name=definition.model or settings.survey_model,
    )
    forge_url = settings.forge_remote_url or "(not configured)"
    prompt = (
        f"<forge_remote_url>{forge_url}</forge_remote_url>\n\n"
        f"<memory>\n{memory or '(empty — start a new ledger)'}\n</memory>\n\n"
        "Survey similar open-source projects and return your proposals."
    )
    from .retry import call_with_retry

    try:
        result = call_with_retry(
            lambda: agent.run_sync(prompt), settings=settings, what="survey"
        )
    finally:
        _safe_close(agent)
    result.output.draft_titles = result.output.draft_titles[:MAX_GAPS]
    result.output.draft_bodies = result.output.draft_bodies[:MAX_GAPS]
    result.output.gap_ids = result.output.gap_ids[:MAX_GAPS]
    return result.output
