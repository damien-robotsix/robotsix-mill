"""The test-gap agent: dedicated test-coverage oversight.

Identifies modules with zero dedicated test coverage, prioritizes by
complexity, I/O surface, and state-transition logic, and proposes draft
tickets for the highest-priority gaps.

Seam: tests monkeypatch ``run_test_gap_agent``. Structured output so
the runner has a clear result to work with.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..config import Settings

SYSTEM_PROMPT = """\
You are a test-gap agent for an autonomous software project. Your sole
job is to identify modules that lack dedicated unit test coverage and
prioritize them for remediation.

INSPECTION METHOD:

- Cross-reference all `src/robotsix_mill/**/*.py` modules against
  `tests/test_*.py` files using `list_dir`, `explore`, and `read_file`.
- Identify modules with **zero dedicated test coverage** (no
  corresponding `test_<module>.py` file).
- For modules tested only **indirectly** (through integration/stage
  tests), assess whether dedicated unit tests would add safety. Exclude
  ABCs <60 lines and trivial wrappers (<30 lines).
- **Prioritize** by:
  (a) module complexity (line count, function count via `explore`),
  (b) I/O surface (file system, network, subprocess calls),
  (c) state-transition or error-handling logic.

HEURISTICS:

- **Critical**: >50 lines AND I/O or state logic AND zero tests → emit
  draft on first pass.
- **High**: >30 lines, zero tests → emit draft on second consecutive
  pass (record in memory ledger).
- **Indirect-only**: tested only through integration tests AND >80
  lines → flag for review.
- **Exclude**: ABCs <60 lines, stubs (e.g. `gitlab.py`), trivial
  wrappers.

DRAFT FORMAT:

- **Title**: `test gap: add unit tests for <module_path>` (e.g.
  `test gap: add unit tests for agents/refining.py`).
- **Body**: module line count, function list (from `explore`),
  suggested test approach (mock seam vs pure unit), existing indirect
  coverage notes.

MEMORY LEDGER:

You are given the current test-gap memory ledger — a Markdown document
that tracks gaps that have been proposed (as draft tickets), declined,
or already addressed (done). The memory is *yours* — you own its
structure and content.

1. **BEFORE proposing new gaps**, reconcile your memory ledger against
   the `## Prior proposals — verified state` table in your input:
   - Items whose ticket reached CLOSED with resolution
     `merged` → move to `## Done` (or equivalent), include the
     ticket_id.
   - Items whose ticket reached CLOSED with resolution
     `declined` → move to `## Declined`, include a brief note.
   - Items with resolution `in-flight` → leave in `## Proposals`.
   - Do **not** re-propose anything that appears as Done or Declined.
2. Inspect the repository using `list_dir`, `explore`, and `read_file`
   as your primary tools. Use `web_research` sparingly — only for
   external best-practice references on testing approaches.
3. Compare findings against the memory ledger. Skip issues already
   recorded (proposed, declined, or done).
4. For each NEW, worthwhile gap, decide whether it merits a draft
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
"""

MAX_GAPS = 5


class TestGapResult(BaseModel):
    updated_memory: str = ""
    draft_titles: list[str] = Field(default_factory=list)
    draft_bodies: list[str] = Field(default_factory=list)
    gap_ids: list[str] = Field(default_factory=list)


def run_test_gap_agent(
    *,
    settings: Settings,
    memory: str = "",
    repo_dir=None,
) -> TestGapResult:
    """Run the test-gap coverage inspection pass.

    Inspects the repository for modules with zero dedicated test
    coverage and returns a structured ``TestGapResult`` with draft
    tickets for newly-discovered gaps.

    When ``repo_dir`` is provided, the agent gets filesystem tools
    (``read_file``, ``list_dir``) and the ``explore`` scout tool so
    it can inspect the actual codebase.  Without ``repo_dir`` the
    agent runs in a read-only reasoning mode (no repo access).

    The agent is constructed via :func:`~.base.build_agent` with the
    role-specific ``SYSTEM_PROMPT``, structured output type
    ``PromptedOutput(TestGapResult)`` (for provider compatibility),
    ``web=True`` (for the ``web_research`` sub-agent tool), and
    ``model_name=settings.test_gap_model``.

    Execution is wrapped in :func:`~.retry.call_with_retry`, which
    handles transient network/model failures (exponential backoff:
    2s base, 30s cap) and ``UsageLimitExceeded`` rate-limit errors
    (30s base, 120s cap with provider fallback after
    ``settings.rate_limit_fallback_retries`` consecutive failures).

    Args:
        settings: Application configuration — model names
            (``test_gap_model``), retry parameters, forge URL, and
            tool paths.
        memory: The agent's memory ledger as a Markdown string.
            Defaults to ``""`` (the agent starts a fresh ledger).
        repo_dir: Optional path to the local repository clone.
            When not ``None``, enables the ``explore``,
            ``read_file``, and ``list_dir`` tools.

    Returns:
        A ``TestGapResult`` with draft titles, bodies, and gap IDs
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
        output_type=PromptedOutput(TestGapResult),
        tools=tools,
        web=True,
        report_issue=False,
        model_name=settings.test_gap_model,
        name="test-gap",
    )
    forge_url = settings.forge_remote_url or "(not configured)"
    prompt = (
        f"<forge_remote_url>{forge_url}</forge_remote_url>\n\n"
        f"<memory>\n{memory or '(empty — start a new ledger)'}\n</memory>\n\n"
        "Perform the test-gap inspection and return your result."
    )
    from .retry import call_with_retry

    try:
        result = call_with_retry(
            lambda: agent.run_sync(prompt), settings=settings, what="test-gap"
        )
    finally:
        _safe_close(agent)
    result.output.draft_titles = result.output.draft_titles[:MAX_GAPS]
    result.output.draft_bodies = result.output.draft_bodies[:MAX_GAPS]
    result.output.gap_ids = result.output.gap_ids[:MAX_GAPS]
    return result.output
