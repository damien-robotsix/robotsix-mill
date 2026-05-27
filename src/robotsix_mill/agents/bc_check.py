"""The bc-check agent: scans the repository for backward-compatibility
shims, no-op compat entry points, legacy property accessors, alias
assignments, default-arg compat branches, and legacy shape fallbacks —
then files draft tickets proposing cleanup for those that are ripe for
removal.

Seam: tests monkeypatch ``run_bc_check_agent``. Structured output so the
runner has a clear result to work with.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from ..config import Settings
from .prompt_blocks import section

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)
import yaml as _yaml
_SYSPROMPT_PATH = Path(__file__).parent.parent.parent.parent / "agent_definitions" / "bc_check.yaml"
SYSTEM_PROMPT: str = _yaml.safe_load(_SYSPROMPT_PATH.read_text())["system_prompt"]


MAX_GAPS = 12


class BcCheckResult(BaseModel):
    updated_memory: str = ""
    draft_titles: list[str] = Field(default_factory=list)
    draft_bodies: list[str] = Field(default_factory=list)
    gap_ids: list[str] = Field(default_factory=list)


def run_bc_check_agent(
    *,
    settings: Settings,
    memory: str = "",
    recent_proposals: str = "",
    repo_dir=None,
) -> BcCheckResult:
    """Run the backward-compatibility inspection pass.

    Scans the repository for backward-compatibility shims, determines
    which are ripe for removal, and returns a structured
    ``BcCheckResult`` with draft tickets.

    When ``repo_dir`` is provided, the agent gets filesystem tools
    (``read_file``, ``list_dir``) and the ``explore`` scout tool so
    it can inspect the actual codebase.  Without ``repo_dir`` the
    agent runs in a read-only reasoning mode (no repo access).

    The agent is constructed via :func:`~.base.build_agent` with the
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
    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent / "agent_definitions" / "bc_check.yaml"
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

    agent = build_agent_from_definition(
        settings, definition, tools=tools,
        model_name=definition.model or settings.bc_check_model,
    )
    prompt = (
        f"{recent_proposals}"
        + section("memory", memory or '(empty — start a new ledger)') + "\n\n"
        + "Scan the repository for backward-compatibility code and return your findings."
    )
    from .retry import call_with_retry

    try:
        result = call_with_retry(
            lambda: agent.run_sync(prompt), settings=settings, what="bc_check"
        )
    finally:
        _safe_close(agent)
    result.output.draft_titles = result.output.draft_titles[:MAX_GAPS]
    result.output.draft_bodies = result.output.draft_bodies[:MAX_GAPS]
    result.output.gap_ids = result.output.gap_ids[:MAX_GAPS]
    return result.output
