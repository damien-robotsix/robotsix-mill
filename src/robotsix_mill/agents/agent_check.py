"""The agent-check agent: inspects agent definitions for internal
coherence — tool–prompt mismatches, skill drift, metadata correctness,
registration completeness, and prompt self-consistency.

Seam: tests monkeypatch ``run_agent_check_agent``. Structured output so
the runner has a clear result to work with.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ConfigDict

from ..config import Settings
from .periodic_base import PeriodicAgentResult, load_periodic_system_prompt

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)
SYSTEM_PROMPT: str = load_periodic_system_prompt("agent_check")

MAX_GAPS = 10


class AgentCheckResult(PeriodicAgentResult):
    """Structured result of an agent-definition coherence pass.

    Extends :class:`~.periodic_base.PeriodicAgentResult` (which carries
    the updated memory ledger and the parallel draft-ticket lists) with
    a ``findings`` field holding the human-readable narrative of the
    coherence issues the agent surfaced.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    findings: str = ""


def run_agent_check_agent(
    *,
    settings: Settings,
    repo_dir=None,
    definition_override=None,
    memory_dir: Path | None = None,
    memory: str = "",
    recent_proposals: str = "",
    verified_proposals: str = "",
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
        definition_override=definition_override,
        max_gaps=MAX_GAPS,
        repo_dir=repo_dir,
        memory=memory,
        recent_proposals=recent_proposals,
        verified_proposals=verified_proposals,
        prompt_tail="Inspect all agent definitions and return your coherence findings.",
        extra_roots=extra_roots,
    )
