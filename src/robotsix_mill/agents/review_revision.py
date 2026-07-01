"""Review-revision agent: implements human-requested changes on a PR branch.

Reads the review comments + current file contents from the ticket's
workspace clone, makes the requested changes, runs local tests, and
commits. Returns ``DONE`` iff changes were applied successfully.

This agent operates *only* on the local clone — it never pushes, opens
PRs, or interacts with the forge. The caller (merge stage) decides
whether to force-push the result.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from ..config import Settings, get_secrets
from .prompt_blocks import section


class ReviewRevisionResult(BaseModel):
    """Structured output from the review-revision agent."""

    status: Literal["DONE", "FAILED"]
    summary: str
    updated_memory: str = ""


def run_review_revision_agent(
    *,
    settings: Settings,
    repo_dir: Path,
    branch: str,
    review_comments: str,
    pr_files: list[str],
    memory: str = "",
) -> ReviewRevisionResult:
    """Run one review-revision attempt.

    Uses the LLM (pydantic-ai agent) with sandboxed file + shell tools
    scoped to *repo_dir*.  The agent reads the review comments, inspects
    the relevant files, makes minimal edits to address the reviewer's
    feedback, runs local tests, and commits.  Returns a
    ``ReviewRevisionResult`` with status, summary, and updated memory.

    This is the mockable seam — tests monkeypatch it to avoid real LLM
    and Docker calls.
    """
    if not get_secrets().openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close
    from .fs_tools import build_fs_tools
    from ..data_paths import data_dir

    definition = load_agent_definition(
        data_dir("agent_definitions") / "review_revision.yaml"
    )

    tools = build_fs_tools(Path(repo_dir), settings)

    agent = build_agent_from_definition(
        settings,
        definition,
        repo_dir=Path(repo_dir),  # confine SDK built-in edit tools to the clone
        tools=tools,
    )

    user_prompt = (
        f"A human reviewer has requested changes on PR branch '{branch}' "
        + f"in {repo_dir}. Here are the review comments:\n\n"
        + f"{review_comments}\n\n"
        + f"Changed files in this PR: {', '.join(pr_files)}\n\n"
        + section("memory", memory or "(empty — start a new ledger)")
        + "\n\n"
        + "Follow the system prompt exactly."
    )

    try:
        result = agent.run_sync(user_prompt)
    finally:
        _safe_close(agent)

    return result.output
