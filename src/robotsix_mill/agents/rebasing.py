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

from ..config import Settings


def run_rebase_agent(
    *,
    settings: Settings,
    repo_dir: Path,
    branch: str,
    target: str,
) -> bool:
    """Run one rebase attempt of *branch* onto ``origin/<target>``.

    Uses the LLM (pydantic-ai agent) with sandboxed file + shell tools
    scoped to *repo_dir*.  The agent loops internally to resolve every
    conflict the rebase encounters; it returns ``True`` only when the
    entire rebase finishes cleanly.  One invocation = one attempt.

    This is the mockable seam — tests monkeypatch it to avoid real LLM
    and Docker calls.
    """
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    from .base import build_agent
    from .fs_tools import build_fs_tools

    # Build tools confined to the ticket's own clone.
    tools = build_fs_tools(Path(repo_dir), settings)

    system_prompt = f"""You are a rebase specialist. Your ONLY job:

1. Run:  git fetch origin
2. Run:  git rebase origin/{target}
3. If the rebase applies cleanly, report DONE.
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

After the rebase completes (or you determine it cannot), respond with
EXACTLY one word on its own line: DONE or FAILED.  You may add a brief
explanation after FAILED."""

    agent = build_agent(
        settings,
        system_prompt=system_prompt,
        output_type=str,
        tools=tools,
        web=False,
    )

    result = agent.run_sync(
        f"Rebase branch '{branch}' onto origin/{target} in {repo_dir}. "
        "Follow the system prompt exactly.",
    )

    # pydantic-ai's AgentRunResult exposes `.output` — the old `.data`
    # AttributeError'd every rebase, blocking the ticket after 2 tries.
    output = str(result.output or "").strip()
    return output.upper().startswith("DONE")
