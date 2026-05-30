"""The agent-check agent: inspects agent definitions for internal
coherence — tool–prompt mismatches, skill drift, metadata correctness,
registration completeness, and prompt self-consistency.

Seam: tests monkeypatch ``run_agent_check_agent``. Structured output so
the runner has a clear result to work with.
"""

from __future__ import annotations

from pathlib import Path

import yaml as _yaml
from pydantic import BaseModel, Field

from ..config import Settings

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)
_SYSPROMPT_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "agent_definitions"
    / "periodic"
    / "agent_check.yaml"
)
SYSTEM_PROMPT: str = _yaml.safe_load(_SYSPROMPT_PATH.read_text())["system_prompt"]

MAX_GAPS = 10


class AgentCheckResult(BaseModel):
    findings: str = ""
    updated_memory: str = ""
    draft_titles: list[str] = Field(default_factory=list)
    draft_bodies: list[str] = Field(default_factory=list)
    gap_ids: list[str] = Field(default_factory=list)


def run_agent_check_agent(
    *,
    settings: Settings,
    repo_dir=None,
    memory_dir: Path | None = None,
    memory: str = "",
    recent_proposals: str = "",
) -> AgentCheckResult:
    """Run the agent-definition coherence inspection pass.

    Inspects agent definitions for internal coherence — tool–prompt
    mismatches, skill drift, metadata correctness, registration
    completeness, and prompt self-consistency — and returns a
    structured ``AgentCheckResult`` with draft tickets.

    The agent is constructed via
    :func:`~.periodic_base.run_periodic_agent`.

    Args:
        settings: Application configuration.
        repo_dir: Optional path to the local repository clone.
        memory_dir: Optional extra root passed to ``build_fs_tools``.
        memory: The agent's memory ledger.
        recent_proposals: Prior proposals from the pass runner.

    Returns:
        An ``AgentCheckResult`` with findings, draft titles, bodies,
        and gap IDs clipped to ``MAX_GAPS`` (10) entries.
    """
    from .periodic_base import run_periodic_agent

    extra_roots = [memory_dir] if memory_dir is not None else None
    return run_periodic_agent(
        settings=settings,
        definition_name="agent_check",
        model_setting=settings.agent_check_model,
        max_gaps=MAX_GAPS,
        repo_dir=repo_dir,
        memory=memory,
        recent_proposals=recent_proposals,
        prompt_tail="Inspect all agent definitions and return your coherence findings.",
        extra_roots=extra_roots,
    )
