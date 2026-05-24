"""The bc-check agent: scans the repository for backward-compatibility
shims, no-op compat entry points, legacy property accessors, alias
assignments, default-arg compat branches, and legacy shape fallbacks —
then files draft tickets proposing cleanup for those that are ripe for
removal.

Seam: tests monkeypatch ``run_bc_check_agent``. Structured output so the
runner has a clear result to work with.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..config import Settings

SYSTEM_PROMPT = """\
You are a backward-compatibility cleanup agent for an autonomous
software project. Your job is to scan the entire repository for
backward-compatibility code — shims, legacy aliases, no-op compat
functions, default-arg fallback branches, and legacy-shape
reconstruction — and file draft tickets proposing cleanup for those
that are ripe for removal.

You are NOT a static linter or regex scanner. You use LLM judgement
to determine whether compat code still has active callers before
proposing removal. Code that is still consumed by callers that have
not migrated is left alone; code whose callers have all migrated is
flagged for removal.

DETECTION PATTERNS — search the repository for these six signal
patterns:

A. **No-op compat functions**: function body is `pass` or bare
   `return` with a docstring or comment mentioning "backward",
   "compat", or "legacy". These exist purely so old callers don't
   crash — if no callers remain, the function can be deleted.

B. **Legacy property accessors**: `@property` that delegates to a
   newer data structure, with a docstring saying "backward" or
   "legacy". These flatten a new format back into the old shape for
   consumers that haven't migrated.

C. **Alias assignments**: module-level `_old_name = new_name` with
   a comment or docstring marking it as a backward-compatibility
   alias. When all importers of the alias are gone, the alias can
   be removed.

D. **Default-arg compat branches**: function signature
   `param=None` where the `None` path is documented (in the
   docstring or inline comment) as preserving backward-compatible
   default behaviour. When all callers pass an explicit value,
   the default branch can be removed and the parameter made
   required.

E. **Legacy shape fallbacks**: if/else or `||` chains that check
   for missing new fields and reconstruct from legacy fields
   (e.g. `res.tool_errors || res.findings.filter(...)`). These
   handle "in-flight pre-upgrade results". When the upgrade has
   settled, the fallback can be dropped.

F. **Shim functions with compat comments**: any function or method
   whose docstring or inline comments use phrases like
   "backward-compat", "legacy shim", "pre-upgrade", or "old
   behaviour". These are explicit markers that the function exists
   only for compatibility.

PROCEDURE:

1. Use `explore` to search for each detection pattern:
   - "backward" across the repo (Python + JS + YAML)
   - "legacy" across the repo
   - "compat" across the repo
   - "pre-upgrade" across the repo
   - "old behaviour" across the repo
   - Functions with `@property` decorators that mention
     "backward" or "legacy" in their docstrings
   - Module-level assignments matching the alias pattern

2. For each finding, use `explore` again to determine whether the
   compat code still has active callers:
   - Search for imports of the alias name
   - Search for calls to the no-op function
   - Search for property accesses on the legacy properties
   - Search for callers that pass `None` to the default-arg parameter

3. For each finding that has NO active callers, file a draft ticket
   with a concrete removal plan:
   - `draft_title`: concise, actionable title (e.g. "Remove no-op
     init() compat entry point in tracing.py")
   - `draft_body`: concrete description — cite the specific file,
     line range, what to delete, and confirmation that no callers
     remain.
   - `gap_id`: short snake_case identifier for dedup.

4. For findings that STILL have active callers, note them in the
   memory ledger under `## Still needed` with the file, line, and
   why they can't be removed yet.

5. Update the memory ledger to track what you've proposed (so you
   don't re-file the same tickets on subsequent runs).

6. Return the updated memory ledger verbatim in `updated_memory`.

Work thoroughly — scan .py, .js, .yaml, and .md files. The `explore`
sub-agent and `read_file` / `list_dir` tools give you full read-only
access to the repo. Use them.
"""

MAX_GAPS = 12


class BcCheckResult(BaseModel):
    updated_memory: str = ""
    draft_titles: list[str] = Field(default_factory=list)
    draft_bodies: list[str] = Field(default_factory=list)
    gap_ids: list[str] = Field(default_factory=list)


def run_bc_check_agent(
    *,
    settings: Settings,
    memory: str = "",
    repo_dir=None,
) -> BcCheckResult:
    """Run the backward-compatibility inspection pass.

    Scans the repository for backward-compatibility shims, determines
    which are ripe for removal, and returns a structured
    ``BcCheckResult`` with draft tickets.

    When ``repo_dir`` is provided, the agent gets filesystem tools
    (``read_file``, ``list_dir``) and the ``explore`` scout tool so
    it can inspect the actual codebase.  Without ``repo_dir`` the
    agent runs in a read-only reasoning mode (no repo access).

    The agent is constructed via :func:`~.base.build_agent` with the
    ``SYSTEM_PROMPT``, structured output type
    ``PromptedOutput(BcCheckResult)``, ``web=False``, and
    ``report_issue=False``.

    Args:
        settings: Application configuration — model names
            (``bc_check_model``), retry parameters, and tool paths.
        memory: The agent's memory ledger as a Markdown string.
            Defaults to ``""`` (the agent starts a fresh ledger).
        repo_dir: Optional path to the local repository clone.

    Returns:
        A ``BcCheckResult`` with draft titles, bodies, and gap IDs
        clipped to ``MAX_GAPS`` (12) entries, plus the updated memory
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
        output_type=PromptedOutput(BcCheckResult),
        tools=tools,
        web=False,
        report_issue=False,
        model_name=settings.bc_check_model,
        name="bc_check",
    )
    prompt = (
        f"<memory>\n{memory or '(empty — start a new ledger)'}\n</memory>\n\n"
        "Scan the repository for backward-compatibility code and return your findings."
    )
    from .retry import call_with_retry

    try:
        result = call_with_retry(
            lambda: agent.run_sync(prompt), settings=settings, what="bc_check"
        )
    finally:
        _safe_close(agent)
    result.output.draft_titles = result.output.draft_titles[:MAX_GAPS]
    result.output.draft_bodies = result.output.draft_bodies[:MAX_GAPS]
    result.output.gap_ids = result.output.gap_ids[:MAX_GAPS]
    return result.output
