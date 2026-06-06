"""The completeness-check agent: inspects the repository for incomplete
feature wiring — missing config mappings, missing defaults, routes with
no button, runners with no CLI, and agent files with no caller — then
files draft tickets proposing completion for each discovered gap.

Seam: tests monkeypatch ``run_completeness_check_agent``. Structured
output so the runner has a clear result to work with.
"""

from __future__ import annotations

from ..config import Settings
from .periodic_base import PeriodicAgentResult, load_periodic_system_prompt

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)
SYSTEM_PROMPT: str = load_periodic_system_prompt("completeness_check")


MAX_GAPS = 12


CompletenessCheckResult = PeriodicAgentResult


def run_completeness_check_agent(
    *,
    settings: Settings,
    memory: str = "",
    recent_proposals: str = "",
    verified_proposals: str = "",
    repo_dir=None,
    definition_override=None,
) -> CompletenessCheckResult:
    """Run the feature-completeness inspection pass.

    Scans the repository for incomplete feature wiring, determines
    which gaps are real, and returns a structured
    ``CompletenessCheckResult`` with draft tickets.

    When ``repo_dir`` is provided, the agent gets filesystem tools
    (``read_file``, ``list_dir``) and the ``explore`` scout tool so
    it can inspect the actual codebase.  Without ``repo_dir`` the
    agent runs in a read-only reasoning mode (no repo access).

    The agent is constructed via
    :func:`~.periodic_base.run_periodic_agent` with the
    ``SYSTEM_PROMPT``, structured output type
    ``PromptedOutput(CompletenessCheckResult)``, ``web=False``, and
    ``report_issue=False``.

    Args:
        settings: Application configuration — model names
            (``completeness_check_model``), retry parameters, and
            tool paths.
        memory: The agent's memory ledger as a Markdown string.
            Defaults to ``""`` (the agent starts a fresh ledger).
        repo_dir: Optional path to the local repository clone.

    Returns:
        A ``CompletenessCheckResult`` with draft titles, bodies, and
        gap IDs clipped to ``MAX_GAPS`` (12) entries, plus the
        updated memory ledger.
    """
    from .periodic_base import run_periodic_agent

    return run_periodic_agent(
        settings=settings,
        definition_name="completeness_check",
        definition_override=definition_override,
        model_setting=settings.completeness_check_model,
        max_gaps=MAX_GAPS,
        repo_dir=repo_dir,
        memory=memory,
        recent_proposals=recent_proposals,
        verified_proposals=verified_proposals,
        prompt_tail="Scan the repository for incomplete feature wiring and return your findings.",
    )
