"""The bc-check agent: scans the repository for backward-compatibility
shims, no-op compat entry points, legacy property accessors, alias
assignments, default-arg compat branches, and legacy shape fallbacks —
then files draft tickets proposing cleanup for those that are ripe for
removal.

Seam: tests monkeypatch ``run_bc_check_agent``. Structured output so the
runner has a clear result to work with.
"""

from __future__ import annotations

from ..config import Settings
from .periodic_base import PeriodicAgentResult, load_periodic_system_prompt

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)
SYSTEM_PROMPT: str = load_periodic_system_prompt("bc_check")


MAX_GAPS = 12


BcCheckResult = PeriodicAgentResult


def run_bc_check_agent(
    *,
    settings: Settings,
    memory: str = "",
    recent_proposals: str = "",
    verified_proposals: str = "",
    repo_dir=None,
    definition_override=None,
) -> BcCheckResult:
    """Run the backward-compatibility inspection pass.

    Scans the repository for backward-compatibility shims, determines
    which are ripe for removal, and returns a structured
    ``BcCheckResult`` with draft tickets.

    When ``repo_dir`` is provided, the agent gets filesystem tools
    (``read_file``, ``list_dir``) and the ``explore`` scout tool so
    it can inspect the actual codebase.  Without ``repo_dir`` the
    agent runs in a read-only reasoning mode (no repo access).

    The agent is constructed via
    :func:`~.periodic_base.run_periodic_agent` with the
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
    from .periodic_base import run_periodic_agent

    return run_periodic_agent(
        settings=settings,
        definition_name="bc_check",
        definition_override=definition_override,
        model_setting=settings.bc_check_model,
        max_gaps=MAX_GAPS,
        repo_dir=repo_dir,
        memory=memory,
        recent_proposals=recent_proposals,
        verified_proposals=verified_proposals,
        prompt_tail="Scan the repository for backward-compatibility code and return your findings.",
    )
