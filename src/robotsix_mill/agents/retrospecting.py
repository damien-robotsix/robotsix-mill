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
and a summary of its Langfuse traces (cost, latency, retries, errors),
identify the single most valuable concrete improvement to the
*pipeline/codebase* — a bug, a fragility, wasted retries, or a token/
cost reduction.

Be conservative: only set propose_draft=true when there is a specific,
actionable change worth implementing. Vague observations -> false.
When proposing, write draft_title and draft_body as a normal ticket a
human could approve as-is (problem + concrete change). Always fill
`findings` with your analysis regardless.
"""


class RetrospectResult(BaseModel):
    findings: str
    propose_draft: bool = False
    draft_title: str | None = None
    draft_body: str | None = None


def run_retrospect_agent(
    *,
    settings: Settings,
    ticket_summary: str,
    history_text: str,
    langfuse_summary: str | None,
) -> RetrospectResult:
    from .base import build_agent

    agent = build_agent(
        settings,
        system_prompt=SYSTEM_PROMPT,
        output_type=RetrospectResult,
    )
    lf = langfuse_summary or "(no Langfuse trace data — workflow-only review)"
    prompt = (
        f"<ticket>\n{ticket_summary}\n</ticket>\n\n"
        f"<workflow>\n{history_text}\n</workflow>\n\n"
        f"<langfuse>\n{lf}\n</langfuse>"
    )
    result = agent.run_sync(prompt)
    return result.output
