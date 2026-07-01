"""Run-health reasoning core.

Reasons over a deterministically pre-built RUN-HEALTH DIGEST (failed and
degraded runs grouped by ``(kind, normalized signature)`` across every
board's run registry) and emits high-confidence draft proposals for the
genuine failures, separating them from legitimate empty/no-op runs.

The agent does NOT read the registries itself — the digest is injected by
``runners.run_health_runner``. Built directly via ``build_agent`` (not
``build_agent_from_definition``) on the default/normal model: telling a real
failure apart from a benign empty run needs judgement, and the bounded
digest keeps the input small.
"""

from __future__ import annotations


import yaml as _yaml
from pydantic import BaseModel, Field

from ..config import Settings
from ..data_paths import data_dir

# ---------------------------------------------------------------------------
# Load the static system prompt from the YAML definition
# ---------------------------------------------------------------------------

_SYSPROMPT_PATH = data_dir("agent_definitions") / "periodic" / "run_health.yaml"
SYSTEM_PROMPT: str = _yaml.safe_load(_SYSPROMPT_PATH.read_text())["system_prompt"]


MAX_PROPOSALS = 8


class RunHealthResult(BaseModel):
    """Structured output from the run-health pass.

    ``draft_titles`` / ``draft_bodies`` / ``gap_ids`` are parallel lists
    (one entry per proposal) clipped to ``MAX_PROPOSALS`` by the runner.
    """

    updated_memory: str = ""
    draft_titles: list[str] = Field(default_factory=list)
    draft_bodies: list[str] = Field(default_factory=list)
    gap_ids: list[str] = Field(default_factory=list)


def run_run_health_agent(
    *,
    settings: Settings,
    memory: str = "",
    recent_proposals: str = "",
    digest: str,
) -> RunHealthResult:
    """Run the run-health reasoning pass over a pre-built *digest*.

    Args:
        settings: Application configuration.
        memory: The agent's memory ledger (Markdown).
        recent_proposals: Prior run-health proposals block.
        digest: The deterministic run-health digest (grouped failed/degraded
            run candidates), built by the runner.

    Returns:
        A ``RunHealthResult`` with parallel draft lists (clipped to
        ``MAX_PROPOSALS``) plus the updated memory ledger.
    """
    from pydantic_ai import PromptedOutput

    from .base import build_agent, _safe_close
    from .prompt_blocks import section
    from .retry import run_agent

    agent = build_agent(
        settings,
        system_prompt=SYSTEM_PROMPT,
        output_type=PromptedOutput(RunHealthResult),
        tools=[],
        web_knowledge=False,
        report_issue=False,
        read_ticket=True,
        reply_to_thread=False,
        close_thread=False,
        ask_user=False,
        # No per-agent model decision: real-vs-benign judgement runs on the
        # normal/default tier (llmio resolves it per backend).
        level=2,
        name="run_health",
    )

    prompt = recent_proposals
    prompt += section("memory", memory or "(empty — start a new ledger)")
    prompt += digest
    prompt += (
        "\n\nStudy the digest and emit your RunHealthResult — "
        "one high-confidence draft per genuine failure group only."
    )

    try:
        result = run_agent(
            agent,
            lambda h: h.run_sync(prompt),
            what="run-health",
        )
    finally:
        _safe_close(agent)

    if result is None or getattr(result, "output", None) is None:
        raise RuntimeError(
            "run_health agent produced null output — "
            "likely an infrastructure failure (Claude CLI crash, "
            "timeout, or fallback exhaustion)"
        )

    out: RunHealthResult = result.output
    # Clip parallel lists in lockstep.
    n = min(
        len(out.draft_titles),
        len(out.draft_bodies),
        len(out.gap_ids),
        MAX_PROPOSALS,
    )
    out.draft_titles = out.draft_titles[:n]
    out.draft_bodies = out.draft_bodies[:n]
    out.gap_ids = out.gap_ids[:n]
    return out
