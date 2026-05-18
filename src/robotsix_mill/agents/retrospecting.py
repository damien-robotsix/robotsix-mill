"""The retrospect agent: analyse a finished ticket's workflow + its
Langfuse session and propose a concrete improvement as a draft.

Seam: tests monkeypatch ``run_retrospect_agent``. Structured output so
the stage has a clear spawn decision.
"""

from __future__ import annotations

from pydantic import BaseModel

from ..config import Settings

SYSTEM_PROMPT = """\
You are a retrospective auditor for an autonomous ticket pipeline.
Given a finished ticket's workflow (state history + notes), its spec,
a summary of its Langfuse traces (cost, latency, retries, errors), and
the current retrospect memory (a Markdown ledger of issues observed
across past tickets), do the following:

1. Identify the single most valuable concrete improvement to the
   *pipeline/codebase* — a bug, a fragility, wasted retries, or a
   token/cost reduction. Fill `findings` with your analysis regardless.

2. Write a concise one-sentence `conclusion` summarising the outcome of
   this ticket's audit (distinct from the full findings).

3. Update the `memory` document you are given.  The memory is *yours* —
   you own its structure and content.  For this ticket, record or merge
   its notable observations under the relevant issue, tracking which
   ticket ids exhibited it and how strong the evidence is.  When you
   judge that an issue now has **enough corroboration across enough
   distinct tickets** to act, set propose_draft=true and provide
   draft_title/draft_body.  There is no hard numeric threshold; you
   judge sufficiency and explain your reasoning in the memory.

4. Once you have filed a draft for an issue, record that fact in the
   memory and do **not** re-file the same issue on later tickets.

Be conservative: only set propose_draft=true when there is a specific,
actionable change worth implementing AND the memory shows enough
corroboration. Vague observations -> false.  When proposing, write
draft_title and draft_body as a normal ticket a human could approve
as-is (problem + concrete change).  Always fill `findings` with your
analysis regardless.

Return the full, updated memory document in `updated_memory`.  If the
incoming memory is empty, you are starting a fresh ledger.
"""


class RetrospectResult(BaseModel):
    findings: str
    conclusion: str
    propose_draft: bool = False
    draft_title: str | None = None
    draft_body: str | None = None
    updated_memory: str = ""


def run_retrospect_agent(
    *,
    settings: Settings,
    ticket_summary: str,
    history_text: str,
    langfuse_summary: str | None,
    memory: str = "",
) -> RetrospectResult:
    from .base import build_agent

    # Structured output_type -> pydantic-ai forces tool_choice, which
    # the cheap driver model can't serve (404). Use the strong model.
    agent = build_agent(
        settings,
        system_prompt=SYSTEM_PROMPT,
        output_type=RetrospectResult,
        model_name=settings.deep_model,
    )
    lf = langfuse_summary or "(no Langfuse trace data — workflow-only review)"
    prompt = (
        f"<ticket>\n{ticket_summary}\n</ticket>\n\n"
        f"<workflow>\n{history_text}\n</workflow>\n\n"
        f"<langfuse>\n{lf}\n</langfuse>\n\n"
        f"<memory>\n{memory or '(empty — start a new ledger)'}\n</memory>"
    )
    result = agent.run_sync(prompt)
    return result.output
