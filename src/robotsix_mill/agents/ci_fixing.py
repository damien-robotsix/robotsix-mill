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

from ..config import Settings


def run_ci_fix_agent(
    *,
    settings: Settings,
    repo_dir: Path,
    branch: str,
    failing_summary: str,
) -> bool:
    """Run one CI-fix attempt based on *failing_summary*.

    Uses the LLM (pydantic-ai agent) with sandboxed file + shell tools
    scoped to *repo_dir*.  The agent reads the failing summary, inspects
    the relevant files, makes minimal edits, runs local tests, and
    commits.  Returns ``True`` only when the fix succeeds.  One
    invocation = one attempt.

    This is the mockable seam — tests monkeypatch it to avoid real LLM
    and Docker calls.
    """
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    from .base import build_agent
    from .fs_tools import build_fs_tools

    # Build tools confined to the ticket's own clone.
    tools = build_fs_tools(Path(repo_dir), settings)

    system_prompt = f"""You are a CI-fix specialist. Your ONLY job is to fix
failing remote CI checks on a PR branch.

The failing check summary is provided below. Use it to understand what
is broken, then:

1. Use read_file to inspect the failing files (within {repo_dir} only).
2. Use write_file to make the **minimal code change** to fix the failure.
3. Run the project's local tests to confirm the fix:
   - Infer the right test command from the project (e.g. pytest, npm test,
     make test, cargo test, etc.).  Look at the project structure to
     decide.
4. If tests pass, commit:  git add -A && git commit -q -m "ci: auto-fix
   <brief description>"
5. Report DONE.

IMPORTANT RULES:
- NEVER change unrelated code — only the minimum needed to fix the CI
  failure.
- NEVER push, fetch other remotes, or touch any branch other than the
  current ticket branch ({branch}).
- NEVER run destructive git commands (reset --hard, rebase, etc.).
- If the failure cannot be resolved (e.g. flaky infra test, missing
  secrets, deeper design issue), report FAILED with a short reason.

After the fix completes (or you determine it cannot), respond with
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
        f"CI is failing on branch '{branch}' in {repo_dir}. "
        "Here is the failing check summary:\n\n"
        f"```\n{failing_summary}\n```\n\n"
        "Follow the system prompt exactly.",
    )

    output = str(result.output or "").strip()
    return output.upper().startswith("DONE")
