"""Rebase agent: resolves merge conflicts on a stale PR branch.

Runs ``git fetch origin && git rebase origin/<target>`` inside the
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

from ..config import Settings


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
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    from pydantic_ai import PromptedOutput

    from .base import build_agent, _safe_close
    from .fs_tools import build_fs_tools

    # Build tools confined to the ticket's own clone.
    tools = build_fs_tools(Path(repo_dir), settings)

    system_prompt = f"""You are a rebase specialist. Your ONLY job:

1. Run:  git fetch origin
2. Run:  git rebase origin/{target}
3. If the rebase applies cleanly, report DONE with a brief summary.
4. If there are conflicts:
   - Use read_file to inspect EVERY conflicted file (git will list them).
   - Edit each conflicted file IN PLACE with write_file to resolve the
     conflicts.  Keep the PR's intent — preserve its changes while
     incorporating the target branch's updates.  Do NOT pull in
     unrelated edits from the target branch.
   - After resolving all files, run:  git add <resolved files...>
   - Then run:  git rebase --continue
   - If more conflicts appear, repeat step 4.
   - If git asks you to edit a commit message during --continue, just
     accept the existing message (do NOT change it).

IMPORTANT RULES:
- NEVER run `git rebase --abort` or `git rebase --skip`.
- NEVER push, fetch other remotes, or touch any branch other than the
  current ticket branch ({branch}).
- NEVER change the PR's intent — only merge conflict markers.
- If the rebase cannot be resolved (e.g. unresolvable conflict,
  unexpected git state), report FAILED with a short reason.

## Memory

You are given a `<memory>` block containing a Markdown ledger of
observations from your past rebase runs. It records:
- Common conflict types in this repo
- File-specific merge strategies
- Known brittle areas that frequently conflict

Reference the memory to avoid re-discovering known patterns. After
the rebase, update the memory in your `updated_memory` field:
- Record any new conflict pattern and its resolution strategy
- Note brittle files that frequently conflict
- Record successful merge strategies for specific file types
- Keep entries concise and ticket-ID-qualified
- If nothing new was learned, return the incoming memory unchanged

After the rebase completes (or you determine it cannot), set status to
DONE or FAILED and provide a brief summary."""

    agent = build_agent(
        settings,
        system_prompt=system_prompt,
        output_type=PromptedOutput(RebaseResult),
        tools=tools,
        web=False,
        name="rebase",
    )

    user_prompt = (
        f"Rebase branch '{branch}' onto origin/{target} in {repo_dir}. "
        "Follow the system prompt exactly.\n\n"
        f"<memory>\n{memory or '(empty — start a new ledger)'}\n</memory>"
    )

    try:
        result = agent.run_sync(user_prompt)
    finally:
        _safe_close(agent)

    return result.output
