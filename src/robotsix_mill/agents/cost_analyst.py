"""Cost-analyst reasoning core.

Reasons over a deterministically pre-built COST DIGEST (aggregate
cost-by-stage distribution + four significant trace/ticket specimens) and
emits high-confidence cost-reduction draft proposals.

The agent does NOT crawl Langfuse — the digest is injected by
``runners.cost_analyst_runner``. Built directly via ``build_agent`` (not
``build_agent_from_definition``) on the default/normal model: finding the
right lever needs judgement, and the bounded digest keeps the input small.
"""

from __future__ import annotations

from pathlib import Path

import yaml as _yaml
from pydantic import BaseModel, Field

from ..config import Settings

# ---------------------------------------------------------------------------
# Load the static system prompt from the YAML definition
# ---------------------------------------------------------------------------

_SYSPROMPT_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "agent_definitions"
    / "periodic"
    / "cost_analyst.yaml"
)
SYSTEM_PROMPT: str = _yaml.safe_load(_SYSPROMPT_PATH.read_text())["system_prompt"]


MAX_PROPOSALS = 8


class CostReductionResult(BaseModel):
    """Structured output from the cost-analyst pass.

    ``draft_titles`` / ``draft_bodies`` / ``gap_ids`` are parallel lists
    (one entry per proposal) clipped to ``MAX_PROPOSALS`` by the runner.
    """

    updated_memory: str = ""
    draft_titles: list[str] = Field(default_factory=list)
    draft_bodies: list[str] = Field(default_factory=list)
    gap_ids: list[str] = Field(default_factory=list)


def run_cost_analyst_agent(
    *,
    settings: Settings,
    memory: str = "",
    recent_proposals: str = "",
    digest: str,
) -> CostReductionResult:
    """Run the cost-analyst reasoning pass over a pre-built *digest*.

    Args:
        settings: Application configuration.
        memory: The agent's memory ledger (Markdown).
        recent_proposals: Prior cost-analyst proposals block.
        digest: The deterministic cost digest (aggregate-cost-by-stage +
            significant-specimens sections), built by the runner.

    Returns:
        A ``CostReductionResult`` with parallel draft lists (clipped to
        ``MAX_PROPOSALS``) plus the updated memory ledger.
    """
    from pydantic_ai import PromptedOutput

    from .base import build_agent, _safe_close
    from .prompt_blocks import section
    from .retry import run_agent

    agent = build_agent(
        settings,
        system_prompt=SYSTEM_PROMPT,
        output_type=PromptedOutput(CostReductionResult),
        tools=[],
        web_knowledge=False,
        report_issue=False,
        read_ticket=True,
        reply_to_thread=False,
        close_thread=False,
        ask_user=False,
        # No per-agent model decision: cost-lever judgement runs on the
        # normal/default tier (llmio resolves it per backend).
        model_name=None,
        name="cost_analyst",
    )

    prompt = recent_proposals
    prompt += section("memory", memory or "(empty — start a new ledger)")
    prompt += digest
    prompt += (
        "\n\nStudy the digest and emit your CostReductionResult — "
        "high-confidence, quantified proposals only."
    )

    try:
        result = run_agent(
            agent,
            lambda h: h.run_sync(prompt),
            settings=settings,
            what="cost-analyst",
        )
    finally:
        _safe_close(agent)

    out: CostReductionResult = result.output
    # Clip parallel lists in lockstep.
    n = min(
        len(out.draft_titles),
        len(out.draft_bodies),
        MAX_PROPOSALS,
    )
    out.draft_titles = out.draft_titles[:n]
    out.draft_bodies = out.draft_bodies[:n]
    out.gap_ids = out.gap_ids[:n]
    return out
