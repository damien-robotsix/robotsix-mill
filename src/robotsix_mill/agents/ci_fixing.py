"""CI-fix agent: auto-fixes failing remote CI checks on a PR branch.

Reads the failing check-run summary/details from the forge, inspects
the affected files in the ticket's workspace clone, makes the minimal
code change to fix the failure, runs the project's local tests, and
commits. Returns ``True`` iff the fix was applied successfully.

This agent operates *only* on the local clone — it never pushes, opens
PRs, or interacts with the forge.  The caller (ci_fix stage) decides
whether to force-push the result.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from ..config import Settings


class CiFixResult(BaseModel):
    """Structured output from the CI-fix agent."""

    status: Literal["DONE", "FAILED"]
    summary: str
    updated_memory: str = ""


def run_ci_fix_agent(
    *,
    settings: Settings,
    repo_dir: Path,
    branch: str,
    failing_summary: str,
    memory: str = "",
) -> CiFixResult:
    """Run one CI-fix attempt based on *failing_summary*.

    Uses the LLM (pydantic-ai agent) with sandboxed file + shell tools
    scoped to *repo_dir*.  The agent reads the failing summary, inspects
    the relevant files, makes minimal edits, runs local tests, and
    commits.  Returns a ``CiFixResult`` with status, summary, and
    updated memory.

    This is the mockable seam — tests monkeypatch it to avoid real LLM
    and Docker calls.
    """
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close
    from .fs_tools import build_fs_tools

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent / "agent_definitions" / "ci_fix.yaml"
    )

    # Build tools confined to the ticket's own clone.
    tools = build_fs_tools(Path(repo_dir), settings)

    system_prompt = definition.system_prompt.format(
        repo_dir=repo_dir, branch=branch
    )

    agent = build_agent_from_definition(
        settings, definition, tools=tools,
        system_prompt=system_prompt,
    )

    user_prompt = (
        f"CI is failing on branch '{branch}' in {repo_dir}. "
        "Here is the failing check summary:\n\n"
        f"```\n{failing_summary}\n```\n\n"
        f"<memory>\n{memory or '(empty — start a new ledger)'}\n</memory>\n\n"
        "Follow the system prompt exactly."
    )

    try:
        result = agent.run_sync(user_prompt)
    finally:
        _safe_close(agent)

    return result.output
