"""The health agent: codebase-health inspection for module size,
function length, documentation coverage, test gaps, complexity
hotspots, and dead code.

Seam: tests monkeypatch ``run_health_agent``. Structured output so
the runner has a clear result to work with.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from ..config import Settings
from .prompt_blocks import section

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)
import yaml as _yaml
_SYSPROMPT_PATH = Path(__file__).parent.parent.parent.parent / "agent_definitions" / "periodic" / "health.yaml"
SYSTEM_PROMPT: str = _yaml.safe_load(_SYSPROMPT_PATH.read_text())["system_prompt"]


MAX_GAPS = 8


class HealthResult(BaseModel):
    updated_memory: str = ""
    draft_titles: list[str] = Field(default_factory=list)
    draft_bodies: list[str] = Field(default_factory=list)
    gap_ids: list[str] = Field(default_factory=list)


def run_health_agent(
    *,
    settings: Settings,
    memory: str = "",
    recent_proposals: str = "",
    repo_dir=None,
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

    The agent is constructed via :func:`~.base.build_agent` with the
    role-specific ``SYSTEM_PROMPT``, structured output type
    ``PromptedOutput(HealthResult)`` (for provider compatibility),
    ``web=True`` (for the ``web_research`` sub-agent tool), and
    ``model_name=settings.health_model``.

    Execution is wrapped in :func:`~.retry.call_with_retry`, which
    handles transient network/model failures (exponential backoff:
    2s base, 30s cap) and ``UsageLimitExceeded`` rate-limit errors
    (30s base, 120s cap with provider fallback after
    ``settings.rate_limit_fallback_retries`` consecutive failures).

    .. note::

        The tool-building pipeline (``make_explore_tool`` + filtered
        ``build_fs_tools`` → ``build_agent``) is duplicated verbatim
        in :func:`~.auditing.run_audit_agent`.  This is tracked under
        the ``audit_health_duplication`` hotspot (see
        :mod:`~.agent_check`).  Both agents inspect overlapping
        codebase-health dimensions; changes to the pipeline should
        be made in both places until a shared builder is extracted.

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
    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent / "agent_definitions" / "periodic" / "health.yaml"
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
        definition.system_prompt, load_overlay(repo_dir, "health"),
    )
    agent = build_agent_from_definition(
        settings, definition, tools=tools,
        model_name=definition.model or settings.health_model,
        system_prompt=system_prompt,
    )
    forge_url = settings.forge_remote_url or "(not configured)"
    prompt = (
        f"{recent_proposals}"
        + section("forge-remote-url", forge_url) + "\n\n"
        + section("memory", memory or '(empty — start a new ledger)') + "\n\n"
        + "Perform the health inspection and return your result."
    )
    from .retry import call_with_retry

    try:
        result = call_with_retry(
            lambda: agent.run_sync(prompt), settings=settings, what="health"
        )
    finally:
        _safe_close(agent)
    result.output.draft_titles = result.output.draft_titles[:MAX_GAPS]
    result.output.draft_bodies = result.output.draft_bodies[:MAX_GAPS]
    result.output.gap_ids = result.output.gap_ids[:MAX_GAPS]
    return result.output
