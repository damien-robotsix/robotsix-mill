"""The health agent: codebase-health inspection for module size,
function length, documentation coverage, test gaps, complexity
hotspots, and dead code.

Seam: tests monkeypatch ``run_health_agent``. Structured output so
the runner has a clear result to work with.
"""

from __future__ import annotations

from ..config import Settings
from .periodic_base import PeriodicAgentResult, load_periodic_system_prompt

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)
SYSTEM_PROMPT: str = load_periodic_system_prompt("health")


MAX_GAPS = 8


HealthResult = PeriodicAgentResult


def run_health_agent(
    *,
    settings: Settings,
    memory: str = "",
    recent_proposals: str = "",
    verified_proposals: str = "",
    repo_dir=None,
    definition_override=None,
) -> HealthResult:
    """Run the codebase-health inspection pass.

    Inspects the repository across eight dimensions — module size,
    function length, documentation coverage, test gaps, complexity
    hotspots, dead code, test-suite organization, and documentation
    structure — and returns a structured
    ``HealthResult`` with draft tickets for newly-discovered gaps.

    When ``repo_dir`` is provided, the agent gets filesystem tools
    (``read_file``, ``list_dir``) and the ``explore`` scout tool so
    it can inspect the actual codebase.  Without ``repo_dir`` the
    agent runs in a read-only reasoning mode (no repo access).

    The agent is constructed via
    :func:`~.periodic_base.run_periodic_agent` with the role-specific
    ``SYSTEM_PROMPT``, structured output type
    ``PromptedOutput(HealthResult)`` (for provider compatibility),
    no web tool (``web_knowledge: false`` — codebase-health inspection
    is answerable entirely from the local clone via explore/read_file/
    list_dir; web access made the agent web-search the project's own
    files and burn its request budget), and
    ``model_name=settings.health_model``.

    Execution is wrapped in :func:`~.retry.call_with_retry`, which
    handles transient network/model failures (exponential backoff:
    2s base, 30s cap) and ``UsageLimitExceeded`` rate-limit errors
    (30s base, 120s cap with provider fallback after
    ``settings.rate_limit_fallback_retries`` consecutive failures).

    Args:
        settings: Application configuration — model names
            (``health_model``), retry parameters, forge URL, and
            tool paths.
        memory: The agent's memory ledger as a Markdown string.
            Defaults to ``""`` (the agent starts a fresh ledger).
        repo_dir: Optional path to the local repository clone.
            When not ``None``, enables the ``explore``,
            ``read_file``, and ``list_dir`` tools.

    Returns:
        A ``HealthResult`` with draft titles, bodies, and gap IDs
        clipped to ``MAX_GAPS`` (8) entries, plus the updated memory
        ledger.
    """
    from .periodic_base import run_periodic_agent

    return run_periodic_agent(
        settings=settings,
        definition_name="health",
        definition_override=definition_override,
        model_setting=settings.health_model,
        max_gaps=MAX_GAPS,
        repo_dir=repo_dir,
        memory=memory,
        recent_proposals=recent_proposals,
        verified_proposals=verified_proposals,
        prompt_tail="Perform the health inspection and return your result.",
        include_forge_url=True,
    )
