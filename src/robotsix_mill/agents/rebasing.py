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

    from .fs_tools import build_fs_tools
    from .yaml_loader import load_and_run_agent

    # Build tools confined to the ticket's own clone.
    tools = build_fs_tools(Path(repo_dir), settings)

    user_prompt = (
        f"Rebase branch '{branch}' onto origin/{target} in {repo_dir}. "
        + "Follow the system prompt exactly.\n\n"
        + section("memory", memory or "(empty — start a new ledger)")
    )

    result = load_and_run_agent(
        settings=settings,
        definition_name="rebase",
        tools=tools,
        prompt=user_prompt,
        what="rebase",
        repo_dir=Path(repo_dir),
        system_prompt_format_kwargs={
            "repo_dir": repo_dir,
            "target": target,
            "branch": branch,
        },
    )

    return result.output
