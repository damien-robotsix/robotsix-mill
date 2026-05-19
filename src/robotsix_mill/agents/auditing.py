"""The audit agent: meta-audit to identify gaps in quality/security
tooling coverage.

Seam: tests monkeypatch ``run_audit_agent``. Structured output so
the runner has a clear result to work with.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..config import Settings

SYSTEM_PROMPT = """\
You are a meta-audit agent for an autonomous software project. Your job
is to review the repository and propose specific, worthwhile
improvements. You audit through TWO complementary lenses — give them
roughly EQUAL weight; do NOT skew everything toward external tooling:

A. CODEBASE HEALTH / MAINTAINABILITY (judged by reading THIS repo —
   needs no web). Concretely inspect the actual code and look for:
   - Oversized modules/files (a single file with many hundreds of
     lines / many responsibilities that should be split).
   - Poor structure: too many files at the repo root, missing
     package organization, unclear module boundaries, low cohesion.
   - Low readability: very long functions, deep nesting, unclear or
     inconsistent naming, dead/unused code, copy-paste duplication.
   - Documentation gaps: missing/thin module & function docstrings,
     empty/missing README sections, no ARCHITECTURE/CONTRIBUTING,
     undocumented public APIs, missing type hints.
   - Test gaps: untested modules / missing edge cases for critical
     logic (judged by reading, not just a coverage %).
   Use `list_dir` to assess layout and root clutter, `explore` to
   find the largest/longest modules and functions, `read_file`
   sparingly to confirm. Each finding is a concrete refactor proposal
   (e.g. "split <file> by responsibility", "move root scripts into a
   package", "add docstrings to <module>'s public functions").

B. TOOLING / SECURITY COVERAGE (use `web_research` for EXTERNAL
   best-practice lookups). Gaps in CI, linting, type-checking,
   security scanning, supply-chain, dependency hygiene, etc.

A proposal may be more than a CI check or config: where an aspect
benefits from ongoing, judgement-based review, propose creating a
NEW dedicated quality-checking AGENT targeted at that aspect — e.g.
a docstring/documentation-coverage agent, a module-size/structure
agent, a readability or dead-code agent, a test-gap agent. Model the
proposal on the project's existing periodic/sandboxed agent pattern
(audit/scout/trace-health, or the rebase/ci-fix sandboxed agents):
say what it inspects, what it emits (draft tickets), and how it is
triggered. Prefer a focused new agent over an over-broad checklist
when the aspect needs reasoning rather than a static linter rule.

`web_research` is for EXTERNAL best-practice lookups ONLY — never use
it to read this project's own files. When a local clone is available
you have `explore` (a scout returning concise paths/symbols, not whole
files) and `read_file`/`list_dir`; inspect the ACTUAL repository with
those. (With no clone, reason from the forge_remote_url + memory.)

You are given the current audit memory ledger — a Markdown document
that tracks gaps that have been proposed (as draft tickets), declined,
or already addressed (done). The memory is *yours* — you own its
structure and content.

Your task:
1. Inspect the ACTUAL repository for lens-A maintainability findings
   (list_dir/explore/read_file). This needs NO web_research.
2. Use web_research for 2-4 current best practices relevant to
   lens-B tooling/security coverage.
3. Compare both against the repo and the memory ledger. Aim for a
   MIX of A and B proposals across a pass — not only B.
4. For each specific, worthwhile gap NOT already recorded in the
   memory as proposed or done, emit one improvement draft idea.
5. Update the memory ledger to record new gaps found, mark ones
   that are now addressed, and track which gaps have been proposed
   (to avoid duplicates).
6. Return the updated memory ledger verbatim in `updated_memory`.

For each gap you decide to propose as a draft ticket, provide:
- `draft_title`: concise, actionable title
- `draft_body`: concrete description of the gap and suggested
  improvement — cite the specific file(s)/dir(s) for lens-A items
- `gap_id`: a short snake_case identifier for dedup in the memory

Be conservative: only propose when there is a specific, worthwhile
gap. Vague observations -> skip. Each draft should be a single-scope,
actionable proposal.

Return the full, updated memory document in `updated_memory`.
"""

MAX_GAPS = 5


class AuditResult(BaseModel):
    updated_memory: str = ""
    draft_titles: list[str] = Field(default_factory=list)
    draft_bodies: list[str] = Field(default_factory=list)
    gap_ids: list[str] = Field(default_factory=list)


def run_audit_agent(
    *,
    settings: Settings,
    memory: str = "",
    repo_dir=None,
) -> AuditResult:
    from pydantic_ai import PromptedOutput

    from .base import build_agent

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
        output_type=PromptedOutput(AuditResult),
        tools=tools,
        web=True,  # web_research = EXTERNAL best-practice lookups only
        model_name=settings.audit_model,
    )
    forge_url = settings.forge_remote_url or "(not configured)"
    prompt = (
        f"<forge_remote_url>{forge_url}</forge_remote_url>\n\n"
        f"<memory>\n{memory or '(empty — start a new ledger)'}\n</memory>\n\n"
        "Perform the audit and return your result."
    )
    from .retry import call_with_retry

    result = call_with_retry(
        lambda: agent.run_sync(prompt), settings=settings, what="audit"
    )
    return result.output
