"""The module-curator agent: codebase-organization oversight.

Compares the live directory tree against ``docs/modules.yaml`` and
emits draft tickets for detected drift — unclassified files, stale
paths, and new-module proposals — including reorganization
opportunities toward the per-module layout
(``src/<module>``, ``docs/<module>``, ``tests/<module>``). System
prompt and schema are loaded from
``agent_definitions/periodic/module_curator.yaml``.

Seam: tests monkeypatch ``run_module_curator_agent``. Structured
output (``ModuleCuratorResult``) so the runner has a clear result
to work with; draft lists are clipped to ``MAX_DRAFTS`` (20).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from ..config import Settings
from ..runners.pass_runner import ProposedActionItem
from .periodic_base import load_periodic_system_prompt
from .prompt_blocks import section

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)
SYSTEM_PROMPT: str = load_periodic_system_prompt("module_curator")


MAX_DRAFTS = 20


class ModuleCuratorResult(BaseModel):
    updated_memory: str = ""
    draft_titles: list[str] = Field(default_factory=list)
    draft_bodies: list[str] = Field(default_factory=list)
    gap_ids: list[str] = Field(default_factory=list)
    proposed_actions: list[ProposedActionItem] = Field(default_factory=list)


def run_module_curator_agent(
    *,
    settings: Settings,
    memory: str = "",
    recent_proposals: str = "",
    verified_proposals: str = "",
    repo_dir=None,
    definition_override=None,
) -> ModuleCuratorResult:
    """Run the module-curator pass.

    Compares the live directory tree against ``docs/modules.yaml``,
    identifies drift (unclassified files, stale paths, new-module
    proposals), and returns a structured ``ModuleCuratorResult`` with
    draft tickets.

    When ``repo_dir`` is provided, the agent gets filesystem tools
    (``read_file``, ``list_dir``) and the ``explore`` scout tool so
    it can inspect the actual codebase.  Without ``repo_dir`` the
    agent runs in a read-only reasoning mode (no repo access).

    Args:
        settings: Application configuration — model names
            (``module_curator_model``), retry parameters, and tool paths.
        memory: The agent's memory ledger as a Markdown string.
            Defaults to ``""`` (the agent starts a fresh ledger).
        repo_dir: Optional path to the local repository clone.

    Returns:
        A ``ModuleCuratorResult`` with draft titles, bodies, and gap IDs
        clipped to ``MAX_DRAFTS`` (20) entries, plus the updated memory
        ledger.
    """
    from .base import build_agent_from_definition, _safe_close

    if definition_override is not None:
        definition = definition_override
    else:
        from .yaml_loader import load_agent_definition

        definition = load_agent_definition(
            Path(__file__).parent.parent.parent.parent
            / "agent_definitions"
            / "periodic"
            / "module_curator.yaml"
        )

    tools: list = []
    if repo_dir is not None:
        from .explore import make_explore_tool
        from .fs_tools import build_fs_tools

        ro = [
            t
            for t in build_fs_tools(repo_dir, settings)
            if t.__name__ in ("read_file", "list_dir", "run_command")
        ]
        tools = [make_explore_tool(settings, repo_dir), *ro]

    if definition_override is not None:
        system_prompt = definition.system_prompt
    else:
        from .overlays import apply_overlay, load_overlay

        system_prompt = apply_overlay(
            definition.system_prompt,
            load_overlay(repo_dir, "module_curator"),
        )
    agent = build_agent_from_definition(
        settings,
        definition,
        tools=tools,
        model_name=definition.model or settings.module_curator_model,
        system_prompt=system_prompt,
    )
    verified_block = ("\n\n" + verified_proposals) if verified_proposals else ""
    prompt = (
        f"{recent_proposals}"
        + verified_block
        + section("memory", memory or "(empty — start a new ledger)")
        + "\n\n"
        + "Read docs/modules.yaml, walk the repo tree, and file draft tickets "
        "for any detected drift — including reorganization opportunities toward "
        "the per-module layout (src/<module>, docs/<module>, tests/<module>)."
    )
    from .retry import run_agent

    try:
        result = run_agent(
            agent,
            lambda h: h.run_sync(prompt),
            settings=settings,
            what="module_curator",
        )
    finally:
        _safe_close(agent)
    result.output.draft_titles = result.output.draft_titles[:MAX_DRAFTS]
    result.output.draft_bodies = result.output.draft_bodies[:MAX_DRAFTS]
    result.output.gap_ids = result.output.gap_ids[:MAX_DRAFTS]
    return result.output
