"""Rebase agent: resolves merge conflicts on a stale PR branch.

Runs ``git rebase origin/<target>`` inside the
ticket's existing workspace clone. On conflict it reads the conflicted
files, edits them in place, and continues the rebase.  Returns ``True``
iff the rebase completed cleanly.

This agent operates *only* on the local clone — it never pushes, opens
PRs, or interacts with the forge.  The caller (merge stage) decides
whether to force-push the result.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from ..config import Settings, get_secrets
from .prompt_blocks import section


class RebaseResult(BaseModel):
    """Structured output from the rebase agent."""

    status: Literal["DONE", "FAILED"]
    summary: str
    updated_memory: str = ""


def run_rebase_agent(
    *,
    settings: Settings,
    repo_dir: Path,
    branch: str,
    target: str,
    memory: str = "",
) -> RebaseResult:
    """Run one rebase attempt of *branch* onto ``origin/<target>``.

    Uses the LLM (pydantic-ai agent) with sandboxed file + shell tools
    scoped to *repo_dir*.  The agent loops internally to resolve every
    conflict the rebase encounters; it returns a ``RebaseResult``
    with status, summary, and updated memory.

    This is the mockable seam — tests monkeypatch it to avoid real LLM
    and Docker calls.
    """
    if not get_secrets().openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close
    from .fs_tools import build_fs_tools

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent / "agent_definitions" / "rebase.yaml"
    )

    # Build tools confined to the ticket's own clone.
    tools = build_fs_tools(Path(repo_dir), settings)

    system_prompt = definition.system_prompt.format(
        repo_dir=repo_dir,
        target=target,
        branch=branch,
    )

    agent = build_agent_from_definition(
        settings,
        definition,
        repo_dir=Path(repo_dir),  # confine SDK built-in edit tools to the clone
        tools=tools,
        system_prompt=system_prompt,
    )

    user_prompt = (
        f"Rebase branch '{branch}' onto origin/{target} in {repo_dir}. "
        + "Follow the system prompt exactly.\n\n"
        + section("memory", memory or "(empty — start a new ledger)")
    )

    # Invoke via run_agent (not a bare agent.run_sync) so the Claude→DeepSeek
    # FallbackAgentHandle actually falls back: the fallback is driven by
    # run_agent, never by FallbackAgentHandle.run_sync. A bare run_sync here
    # meant a Claude outage (e.g. exhausted credit) hard-failed the rebase and
    # blocked the ticket instead of falling back like every other stage.
    from .retry import run_agent

    try:
        result = run_agent(
            agent,
            lambda h: h.run_sync(user_prompt),
            settings=settings,
            what="rebase",
        )
    finally:
        _safe_close(agent)

    return result.output
