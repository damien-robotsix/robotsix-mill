"""The test-gap agent: dedicated test-coverage oversight.

Identifies modules with zero dedicated test coverage, prioritizes by
complexity, I/O surface, and state-transition logic, and proposes draft
tickets for the highest-priority gaps.

Seam: tests monkeypatch ``run_test_gap_agent``. Structured output so
the runner has a clear result to work with.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from ..config import Settings
from ..runners.pass_runner import ProposedActionItem

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)
import yaml as _yaml

_SYSPROMPT_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "agent_definitions"
    / "periodic"
    / "test_gap.yaml"
)
SYSTEM_PROMPT: str = _yaml.safe_load(_SYSPROMPT_PATH.read_text())["system_prompt"]


MAX_GAPS = 5


class TestGapResult(BaseModel):
    updated_memory: str = ""
    summary: str = Field(
        default="",
        description=(
            "One sentence: what you examined and the basis for the number "
            "of drafts filed (e.g. 'scanned 142 files; jscpd found 3 clone "
            "pairs, 0 above the severity threshold'). ALWAYS fill this so "
            "an operator can verify a 0-draft run is legitimate."
        ),
    )
    draft_titles: list[str] = Field(default_factory=list)
    draft_bodies: list[str] = Field(default_factory=list)
    gap_ids: list[str] = Field(default_factory=list)
    proposed_actions: list[ProposedActionItem] = Field(default_factory=list)


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
    from .periodic_base import run_periodic_agent

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
    )
