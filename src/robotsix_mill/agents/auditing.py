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
is to review the repository against current web-sourced best practices
for code quality, security, and developer-experience tooling, and
identify specific, worthwhile gaps that could be addressed by new
tools, agents, checks, or process improvements.

You have access to a web_research tool for looking up current best
practices. You also have access to the repository context via the
forge_remote_url provided.

You are given the current audit memory ledger — a Markdown document
that tracks gaps that have been proposed (as draft tickets), declined,
or already addressed (done). The memory is *yours* — you own its
structure and content.

Your task:
1. Use web_research to identify 3-5 current best practices for repo
   quality/security coverage that are relevant to this project.
2. Compare these against the current repository (infer from context/
   forge_remote_url) and the memory ledger.
3. For each specific, worthwhile gap NOT already recorded in the
   memory as proposed or done, emit one improvement draft idea.
4. Update the memory ledger to record new gaps found, mark ones
   that are now addressed, and track which gaps have been proposed
   (to avoid duplicates).
5. Return the updated memory ledger verbatim in `updated_memory`.

For each gap you decide to propose as a draft ticket, provide:
- `draft_title`: concise, actionable title
- `draft_body`: concrete description of the gap and suggested improvement
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
) -> AuditResult:
    from pydantic_ai import PromptedOutput

    from .base import build_agent

    agent = build_agent(
        settings,
        system_prompt=SYSTEM_PROMPT,
        output_type=PromptedOutput(AuditResult),
        web=True,  # gives web_research tool
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
