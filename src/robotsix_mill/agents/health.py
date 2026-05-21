"""The health agent: codebase-health inspection for module size,
function length, documentation coverage, test gaps, complexity
hotspots, and dead code.

Seam: tests monkeypatch ``run_health_agent``. Structured output so
the runner has a clear result to work with.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..config import Settings

SYSTEM_PROMPT = """\
You are a codebase-health agent for an autonomous software project. Your
job is to inspect the repository and identify specific, worthwhile
health issues across six dimensions. You have judgement — you are not a
static linter. A 600-line data model may be fine; a 400-line function
with deep nesting is not. You track findings over time via a memory
ledger so you don't re-nag about the same issues.

INSPECT THE FOLLOWING SIX DIMENSIONS — aim for balanced coverage across
all of them in each pass:

1. MODULE SIZE — Use `list_dir` and `explore` to identify files over
   500 lines. Flag those that appear to lack clear cohesion (many
   unrelated classes/functions). A large data model or generated file
   may be justified — note that and move on. Focus on modules where the
   size signals a split opportunity.

2. FUNCTION LENGTH — Use `explore` to find functions over 80 lines.
   Consider role: a coordinator/orchestrator function that sequences
   high-level steps may naturally be longer than a pure helper.
   Flag functions where length combines with deep nesting or unclear
   responsibility.

3. DOCUMENTATION COVERAGE — Count public symbols (classes, functions
   with leading underscore excluded) vs. docstrings per module. Flag
   modules where coverage is below ~70% or trending down across memory
   passes. Look at README sections, ARCHITECTURE docs, and module-level
   docstrings.

4. TEST GAPS — Cross-reference `src/` and `tests/` to identify modules
   with no corresponding test file or very thin test coverage. Pay
   special attention to critical modules like those in `agents/`,
   `stages/`, and `core/`. Flag specific untested or under-tested
   modules.

5. COMPLEXITY HOTSPOTS — Inspect modules via `read_file` for deep
   nesting (4+ levels), long condition chains, complex boolean
   expressions, repeated patterns that could be refactored into helper
   functions. Flag the specific file and function.

6. DEAD CODE / UNUSED IMPORTS — Look for functions/classes that are
   never called from anywhere else in the codebase, and imports that
   are never used.

You are given the current health memory ledger — a Markdown document
that tracks issues that have been proposed (as draft tickets),
declined, or already addressed (done). The memory is *yours* — you own
its structure and content.

Your task:
1. **BEFORE proposing new gaps**, reconcile your memory ledger against the `## Prior proposals — verified state` block in your input:
   - Items whose ticket reached CLOSED with resolution `merged` → move to `## Done` (or equivalent), include the ticket_id.
   - Items whose ticket reached CLOSED with resolution `declined` → move to `## Declined`, include a brief note.
   - Items with resolution `in-flight` → leave in `## Proposals`.
   - Do **not** re-propose anything that appears as Done or Declined.
2. Inspect the repository across all six dimensions using `list_dir`,
   `explore`, and `read_file` as your primary tools. Use `web_research`
   sparingly — only for external best-practice references (e.g. "what
   is a reasonable function-length guideline for Python?").
3. Compare findings against the memory ledger. Skip issues already
   recorded (proposed, declined, or done).
4. For each NEW, worthwhile finding, decide whether it merits a draft
   ticket. Be conservative — only file when there is a specific,
   actionable gap. Vague observations are skipped.
5. Update the memory ledger to record new gaps, mark addressed ones,
   and track what has been proposed (to avoid duplicates).
6. Return the updated memory ledger verbatim in `updated_memory`.

For each gap you decide to propose as a draft ticket, provide:
- `draft_title`: concise, actionable title
- `draft_body`: concrete description of the gap and suggested
  improvement — cite the specific file(s)/function(s)
- `gap_id`: a short snake_case identifier for dedup in the memory

Be specific, be judgement-based (not just threshold-based), and stay
focused on genuine maintainability improvements.
"""

MAX_GAPS = 8


class HealthResult(BaseModel):
    updated_memory: str = ""
    draft_titles: list[str] = Field(default_factory=list)
    draft_bodies: list[str] = Field(default_factory=list)
    gap_ids: list[str] = Field(default_factory=list)


def run_health_agent(
    *,
    settings: Settings,
    memory: str = "",
    repo_dir=None,
) -> HealthResult:
    """Run the codebase-health inspection pass.

    Inspects the repository across six dimensions — module size,
    function length, documentation coverage, test gaps, complexity
    hotspots, and dead code — and returns a structured
    ``HealthResult`` with draft tickets for newly-discovered gaps.

    When ``repo_dir`` is provided, the agent gets filesystem tools
    (``read_file``, ``list_dir``) and the ``explore`` scout tool so
    it can inspect the actual codebase.  Without ``repo_dir`` the
    agent runs in a read-only reasoning mode (no repo access).

    The agent is constructed via :func:`~.base.build_agent` with the
    role-specific ``SYSTEM_PROMPT``, structured output type
    ``PromptedOutput(HealthResult)`` (for provider compatibility),
    ``web=True`` (for the ``web_research`` sub-agent tool), and
    ``model_name=settings.health_model``.

    Execution is wrapped in :func:`~.retry.call_with_retry`, which
    handles transient network/model failures (exponential backoff:
    2s base, 30s cap) and ``UsageLimitExceeded`` rate-limit errors
    (30s base, 120s cap with provider fallback after
    ``settings.rate_limit_fallback_retries`` consecutive failures).

    .. note::

        The tool-building pipeline (``make_explore_tool`` + filtered
        ``build_fs_tools`` → ``build_agent``) is duplicated verbatim
        in :func:`~.auditing.run_audit_agent`.  This is tracked under
        the ``audit_health_duplication`` hotspot (see
        :mod:`~.agent_check`).  Both agents inspect overlapping
        codebase-health dimensions; changes to the pipeline should
        be made in both places until a shared builder is extracted.

    Args:
        settings: Application configuration — model names
            (``health_model``), retry parameters, forge URL, and
            tool paths.
        memory: The agent's memory ledger as a Markdown string.
            Defaults to ``""`` (the agent starts a fresh ledger).
        repo_dir: Optional path to the local repository clone.
            When not ``None``, enables the ``explore``,
            ``read_file``, and ``list_dir`` tools.

    Returns:
        A ``HealthResult`` with draft titles, bodies, and gap IDs
        clipped to ``MAX_GAPS`` (8) entries, plus the updated memory
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
        output_type=PromptedOutput(HealthResult),
        tools=tools,
        web=True,  # web_research = EXTERNAL best-practice lookups only
        # No report_issue: this agent emits drafts via PromptedOutput.
        # Same pattern as audit/retrospect/agent_check; having both
        # channels open produces double-filing. Regressed twice — ticket
        # f3d1 fixed it in PR #98, then PR #94 (docstring add on a stale
        # base) auto-merged and silently dropped the kwarg. The test in
        # tests/test_agent_double_emit.py locks the contract in.
        report_issue=False,
        model_name=settings.health_model,
        name="health",
    )
    forge_url = settings.forge_remote_url or "(not configured)"
    prompt = (
        f"<forge_remote_url>{forge_url}</forge_remote_url>\n\n"
        f"<memory>\n{memory or '(empty — start a new ledger)'}\n</memory>\n\n"
        "Perform the health inspection and return your result."
    )
    from .retry import call_with_retry

    try:
        result = call_with_retry(
            lambda: agent.run_sync(prompt), settings=settings, what="health"
        )
    finally:
        _safe_close(agent)
    result.output.draft_titles = result.output.draft_titles[:MAX_GAPS]
    result.output.draft_bodies = result.output.draft_bodies[:MAX_GAPS]
    result.output.gap_ids = result.output.gap_ids[:MAX_GAPS]
    return result.output
