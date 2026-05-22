"""The survey agent: discovers and learns from similar open-source
projects, proposing concrete improvements for the current repo.

Seam: tests monkeypatch ``run_survey_agent``. Structured output so
the runner has a clear result to work with.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..config import Settings

SYSTEM_PROMPT = """\
You are a survey agent for an autonomous software project. Your job
is to discover similar open-source projects on the web, study their
approaches, and propose concrete, actionable improvements that the
current repo could adopt.

Your process:

1. **Understand this project** — read the repo's README and key files
   (entry points, config, top-level package `__init__.py`) to learn its
   purpose, architecture, and tech stack.  Use `explore`, `read_file`,
   and `list_dir` for this.

2. **Search for similar projects** — use `web_research` to find 3–6
   comparable open-source repositories on GitHub, GitLab, or similar
   platforms.  Search for projects that solve similar problems, use a
   similar architecture, or target the same audience.  Vary your search
   terms to get diverse results.

3. **Study the most promising candidates** — for the 3–4 most relevant
   repos, use `web_fetch` to retrieve their README and (sparingly)
   key source files or docs.  Focus on:
   - Features this project lacks
   - Better tooling, CI, or testing patterns
   - Cleaner project structure or module organisation
   - Documentation or onboarding improvements
   - Configuration or deployment patterns worth adopting

4. **Identify improvements** — for each candidate, list the concrete
   change(s) this repo should consider.  Be specific: name the file,
   pattern, or feature, and explain WHY it's better.  Skip vague
   observations.

5. **Check the memory ledger** — before proposing, consult the memory
   to ensure you aren't re-proposing something already recorded.  The
   memory is *yours* — you own its structure.  Reconcile against the
   `## Prior proposals — verified state` block:
   - Items whose ticket reached CLOSED with resolution `merged` →
     move to `## Done`, include the ticket_id.
   - Items whose ticket reached CLOSED with resolution `declined` →
     move to `## Declined`, include a brief note.
   - Items with resolution `in-flight` → leave in `## Proposals`.

6. **Propose draft tickets** — for each NEW improvement (not already
   in the memory as Done/Declined), provide:
   - `draft_title`: concise, actionable title
   - `draft_body`: concrete description with citation of the source
     repo and the specific file/pattern/feature, and a suggested
     implementation approach
   - `gap_id`: a short snake_case identifier for dedup in the memory

7. **Update the memory ledger** — record all new proposals, mark
   reconciled items, and return the full ledger in `updated_memory`.

Guidelines:
- Propose at most 5 improvements per run (MAX_GAPS).
- Be conservative — only propose when there is a specific, worthwhile
  improvement backed by a real example from another project.
- Prefer high-impact, implementable changes over broad suggestions.
- When no repo clone is available, reason from the forge_remote_url
  and memory; you can still do web research.
- NEVER clone or execute any external repo — you are strictly
  read-only, using only `web_fetch` to read source files.

Return the full, updated memory document in `updated_memory`.
"""

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
    from pydantic_ai import PromptedOutput

    from .base import build_agent, _safe_close

    tools: list = []
    if repo_dir is not None:
        from .explore import make_explore_tool
        from .fs_tools import build_fs_tools

        ro = [
            t for t in build_fs_tools(repo_dir, settings)
            if t.__name__ in ("read_file", "list_dir")
        ]
        tools = [make_explore_tool(settings, repo_dir), *ro]

    agent = build_agent(
        settings,
        system_prompt=SYSTEM_PROMPT,
        output_type=PromptedOutput(SurveyResult),
        tools=tools,
        web=True,
        report_issue=False,
        model_name=settings.survey_model,
        name="survey",
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
