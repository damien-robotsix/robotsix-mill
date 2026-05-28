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
from .prompt_blocks import section

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)
import yaml as _yaml
_SYSPROMPT_PATH = Path(__file__).parent.parent.parent.parent / "agent_definitions" / "periodic" / "test_gap.yaml"
SYSTEM_PROMPT: str = _yaml.safe_load(_SYSPROMPT_PATH.read_text())["system_prompt"]


MAX_GAPS = 5


class TestGapResult(BaseModel):
    updated_memory: str = ""
    draft_titles: list[str] = Field(default_factory=list)
    draft_bodies: list[str] = Field(default_factory=list)
    gap_ids: list[str] = Field(default_factory=list)


def run_test_gap_agent(
    *,
    settings: Settings,
    memory: str = "",
    recent_proposals: str = "",
    repo_dir=None,
) -> TestGapResult:
    """Run the test-gap coverage inspection pass.

    Inspects the repository for modules with zero dedicated test
    coverage and returns a structured ``TestGapResult`` with draft
    tickets for newly-discovered gaps.

    When ``repo_dir`` is provided, the agent gets filesystem tools
    (``read_file``, ``list_dir``) and the ``explore`` scout tool so
    it can inspect the actual codebase.  Without ``repo_dir`` the
    agent runs in a read-only reasoning mode (no repo access).

    The agent is constructed via :func:`~.base.build_agent` with the
    role-specific ``SYSTEM_PROMPT``, structured output type
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
    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent / "agent_definitions" / "periodic" / "test_gap.yaml"
    )

    tools: list = []
    if repo_dir is not None:
        from .explore import make_explore_tool
        from .fs_tools import build_fs_tools

        ro = [
            t for t in build_fs_tools(repo_dir, settings)
            if t.__name__ in ("read_file", "list_dir")
        ]
        tools = [make_explore_tool(settings, repo_dir), *ro]

    from .overlays import apply_overlay, load_overlay
    system_prompt = apply_overlay(
        definition.system_prompt, load_overlay(repo_dir, "test_gap"),
    )
    agent = build_agent_from_definition(
        settings, definition, tools=tools,
        model_name=definition.model or settings.test_gap_model,
        system_prompt=system_prompt,
    )
    forge_url = settings.forge_remote_url or "(not configured)"
    prompt = (
        f"{recent_proposals}"
        + section("forge-remote-url", forge_url) + "\n\n"
        + section("memory", memory or '(empty — start a new ledger)') + "\n\n"
        + "Perform the test-gap inspection and return your result."
    )
    from .retry import call_with_retry

    try:
        result = call_with_retry(
            lambda: agent.run_sync(prompt), settings=settings, what="test-gap"
        )
    finally:
        _safe_close(agent)
    result.output.draft_titles = result.output.draft_titles[:MAX_GAPS]
    result.output.draft_bodies = result.output.draft_bodies[:MAX_GAPS]
    result.output.gap_ids = result.output.gap_ids[:MAX_GAPS]
    return result.output
