"""The test-gap agent: dedicated test-coverage oversight.

Identifies modules with zero dedicated test coverage, prioritizes by
complexity, I/O surface, and state-transition logic, and proposes draft
tickets for the highest-priority gaps.

Seam: tests monkeypatch ``run_test_gap_agent``. Structured output so
the runner has a clear result to work with.
"""

from __future__ import annotations

from ..config import Settings
from .periodic_base import PeriodicAgentResult, load_periodic_system_prompt

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)
SYSTEM_PROMPT: str = load_periodic_system_prompt("test_gap")


MAX_GAPS = 5


TestGapResult = PeriodicAgentResult


def run_test_gap_agent(
    *,
    settings: Settings,
    memory: str = "",
    recent_proposals: str = "",
    verified_proposals: str = "",
    repo_dir=None,
    definition_override=None,
) -> TestGapResult:
    """Run the test-gap coverage inspection pass.

    Inspects the repository for modules with zero dedicated test
    coverage and returns a structured ``TestGapResult`` with draft
    tickets for newly-discovered gaps.

    When ``repo_dir`` is provided, the agent gets filesystem tools
    (``read_file``, ``list_dir``) and the ``explore`` scout tool so
    it can inspect the actual codebase.  Without ``repo_dir`` the
    agent runs in a read-only reasoning mode (no repo access).

    The agent is constructed via
    :func:`~.periodic_base.run_periodic_agent` with the role-specific
    ``SYSTEM_PROMPT``, structured output type
    ``PromptedOutput(TestGapResult)`` (for provider compatibility),
    ``web=True`` (for the ``web_research`` sub-agent tool), and
    ``model_name=settings.test_gap_model``.

    Execution is wrapped in :func:`~.retry.call_with_retry`, which
    handles transient network/model failures (exponential backoff:
    2s base, 30s cap) and ``UsageLimitExceeded`` rate-limit errors
    (30s base, 120s cap with provider fallback after
    ``settings.rate_limit_fallback_retries`` consecutive failures).

    Args:
        settings: Application configuration — model names
            (``test_gap_model``), retry parameters, forge URL, and
            tool paths.
        memory: The agent's memory ledger as a Markdown string.
            Defaults to ``""`` (the agent starts a fresh ledger).
        repo_dir: Optional path to the local repository clone.
            When not ``None``, enables the ``explore``,
            ``read_file``, and ``list_dir`` tools.

    Returns:
        A ``TestGapResult`` with draft titles, bodies, and gap IDs
        clipped to ``MAX_GAPS`` (5) entries, plus the updated memory
        ledger.
    """
    from pydantic_ai.usage import UsageLimits

    from .periodic_base import run_periodic_agent

    limits = UsageLimits(request_limit=settings.test_gap_request_limit)
    return run_periodic_agent(
        settings=settings,
        definition_name="test_gap",
        definition_override=definition_override,
        model_setting=settings.test_gap_model,
        max_gaps=MAX_GAPS,
        repo_dir=repo_dir,
        memory=memory,
        recent_proposals=recent_proposals,
        verified_proposals=verified_proposals,
        prompt_tail="Perform the test-gap inspection and return your result.",
        include_forge_url=True,
        usage_limits=limits,
    )
