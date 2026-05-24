"""The agent-check agent: inspects agent definitions for internal
coherence — tool–prompt mismatches, skill drift, metadata correctness,
registration completeness, and prompt self-consistency.

Seam: tests monkeypatch ``run_agent_check_agent``. Structured output so
the runner has a clear result to work with.
"""

from __future__ import annotations

from pathlib import Path

import yaml as _yaml
from pydantic import BaseModel, Field

from ..config import Settings

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)
_SYSPROMPT_PATH = Path(__file__).parent.parent.parent.parent / "agent_definitions" / "agent_check.yaml"
SYSTEM_PROMPT: str = _yaml.safe_load(_SYSPROMPT_PATH.read_text())["system_prompt"]

MAX_GAPS = 10


class AgentCheckResult(BaseModel):
    findings: str = ""
    updated_memory: str = ""
    draft_titles: list[str] = Field(default_factory=list)
    draft_bodies: list[str] = Field(default_factory=list)
    gap_ids: list[str] = Field(default_factory=list)


def run_agent_check_agent(
    *,
    settings: Settings,
    repo_dir=None,
    memory: str = "",
) -> AgentCheckResult:
    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close

    definition = load_agent_definition(_SYSPROMPT_PATH)

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
        model_name=definition.model or settings.agent_check_model,
    )
    prompt = (
        f"<memory>\n{memory or '(empty — start a new ledger)'}\n</memory>\n\n"
        "Inspect all agent definitions and return your coherence findings."
    )
    from .retry import call_with_retry

    try:
        result = call_with_retry(
            lambda: agent.run_sync(prompt), settings=settings, what="agent_check"
        )
    finally:
        _safe_close(agent)
    result.output.draft_titles = result.output.draft_titles[:MAX_GAPS]
    result.output.draft_bodies = result.output.draft_bodies[:MAX_GAPS]
    result.output.gap_ids = result.output.gap_ids[:MAX_GAPS]
    return result.output
