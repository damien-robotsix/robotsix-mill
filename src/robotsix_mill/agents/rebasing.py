"""Rebase agent: resolves merge conflicts on a stale PR branch.

Runs ``git rebase origin/<target>`` inside the
ticket's existing workspace clone. On conflict it reads the conflicted
files, edits them in place, and continues the rebase.

The agent now DRIVES the full push flow via bridged git tools
(``git_fetch``, ``git_remote_sha``, ``git_push_with_lease``,
``git_branch_ancestry``) that the mill executes host-side with
the per-repo token — the agent stays network-isolated and never
sees credentials.  On a lease rejection the agent inspects the
remote ancestry and auto-recovers when the remote only carries
its own prior rebase push (no foreign commits).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ..config import Settings, get_secrets
from .prompt_blocks import section


class RebaseResult(BaseModel):
    """Structured output from the rebase agent."""

    model_config = ConfigDict(strict=True, extra="forbid")

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
    remote_url: str | None = None,
    token: str | None = None,
) -> RebaseResult:
    """Run one rebase attempt of *branch* onto ``origin/<target>``.

    Uses the LLM (pydantic-ai agent) with sandboxed file + shell tools
    scoped to *repo_dir*, plus bridged git tools that execute host-side
    with *remote_url* and *token* so the agent can drive fetch + push.

    The agent loops internally to resolve every conflict the rebase
    encounters; it returns a ``RebaseResult`` with status, summary,
    and updated memory.

    This is the mockable seam — tests monkeypatch it to avoid real LLM
    and Docker calls.
    """
    if not get_secrets().openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    from .bridged_git_tools import build_bridged_git_tools
    from .fs_tools import build_fs_tools
    from .yaml_loader import load_and_run_agent

    # Build sandboxed fs tools confined to the ticket's own clone.
    tools = build_fs_tools(Path(repo_dir), settings)

    # Build bridged git tools (host-side, with per-repo token) so the
    # agent can drive fetch + push without ever seeing credentials.
    # Always built — when remote_url is None (e.g. tests), the tools
    # return clear errors rather than failing silently.
    tools.extend(
        build_bridged_git_tools(
            repo_dir=Path(repo_dir),
            branch=branch,
            target=target,
            remote_url=remote_url or "",
            token=token,
        )
    )

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
