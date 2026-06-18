"""The ``spawn_subtask`` tool for the implement coordinator.

When a ticket decomposes into many atomic, independent edits — the
canonical case is a per-file or per-module batch — the coordinator can
delegate one chunk at a time to a sub-agent with its own request
budget. This lets the parent stay lean (one outer plan + N sub-agent
calls) and keeps each subtask's context bounded to its own files.

Design pins:

- Same tool palette as the parent (read/write/edit/list/explore/
  run_command). The sub-agent runs on the same workspace clone, so
  edits land on the parent's branch directly.
- No ``consult_expert``, no ``spawn_subtask`` (no recursion). One
  level of delegation; the parent owns orchestration.
- Per-subtask request budget set by
  ``settings.subtask_request_limit`` (default 30). The parent's
  overall ``coordinator_request_limit`` is the outer cap, so a
  runaway sub-agent can't starve the parent past it.
- Plain-string output. The sub-agent returns a short summary of what
  it did; the parent uses that to decide on the next subtask.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import Settings

log = logging.getLogger("robotsix_mill.spawn_subtask")


async def run_spawn_subtask(
    *,
    settings: Settings,
    repo_dir: Path,
    name: str,
    prompt: str,
    files_in_scope: list[str] | None = None,
    level: int | None = None,
) -> str:
    """Run one sub-agent pass to completion. Returns its summary.

    Args:
        settings: Mill settings — drives model + budget.
        repo_dir: Workspace clone the sub-agent edits.
        name: Short kebab-case label for the subtask (lands in logs +
            tracing). The parent picks the name so its plan stays
            traceable.
        prompt: The operator-authored subtask spec the parent built.
            The sub-agent sees this as its entire system+user prompt
            payload; treat it as a self-contained mini-spec.
        files_in_scope: Optional list of paths the parent expects the
            sub-agent to touch. Injected into the prompt as a scope
            hint. The sub-agent still has full read/write fs tools —
            this is guidance, not enforcement.
        level: Optional capability-level override; defaults to
            level 2 (the implement tier).

    Returns:
        The sub-agent's final string output (its summary). On budget
        cap or other failure, returns a structured "subtask
        incomplete: …" string so the parent can decide what to do
        instead of raising into the parent's loop.
    """
    from pydantic_ai.exceptions import UsageLimitExceeded
    from pydantic_ai.usage import UsageLimits

    from .base import build_agent, _safe_close
    from .explore import make_explore_tool
    from .fs_tools import build_fs_tools

    fs = build_fs_tools(repo_dir, settings)
    fs_tools = [
        t
        for t in fs
        if t.__name__
        in (
            "read_file",
            "write_file",
            "list_dir",
            "edit_file",
            "delete_file",
            "run_command",
        )
    ]

    system_prompt = (
        f"You are a focused sub-agent spawned by the implement "
        f"coordinator to complete ONE atomic subtask of the parent "
        f"ticket.\n\n"
        f"Subtask: {name}\n\n"
        f"Procedure:\n"
        f"1. Read the subtask spec below.\n"
        f"2. Make the minimum edits the spec asks for. Same fs tools "
        f"the parent has — read/write/edit/list/explore/run_command.\n"
        f"3. When done, return a 1-3 sentence summary of what you "
        f"edited. The parent reads this summary; be specific about "
        f"files touched and what changed.\n\n"
        f"Constraints:\n"
        f"- Do NOT call spawn_subtask (no recursion).\n"
        f"- Do NOT commit / push / touch git — the parent owns version "
        f"control.\n"
        f"- Stay within the files listed in scope (if provided). "
        f"Touching out-of-scope files is fine when truly needed (e.g. "
        f"an import update) but minimise.\n"
        f"- You have your own request budget (typically 30). If you "
        f"can't finish, return a partial summary noting what's done "
        f"and what's left."
    )

    agent = build_agent(
        settings,
        name=f"subtask:{name}",
        system_prompt=system_prompt,
        level=level or 2,
        tools=[make_explore_tool(settings, repo_dir), *fs_tools],
        web_knowledge=False,
        report_issue=False,
        read_ticket=False,
        reply_to_thread=False,
        close_thread=False,
        ask_user=False,
        retries=1,
        skills=[],
        repo_dir=repo_dir,  # confine SDK built-in edits to the workspace clone
    )

    scope_block = ""
    if files_in_scope:
        scope_block = (
            "## files-in-scope\n" + "\n".join(f"- {p}" for p in files_in_scope) + "\n\n"
        )
    user_prompt = (
        f"## subtask-spec\n\n{prompt.strip()}\n\n"
        f"{scope_block}"
        "Edit the files, then return your summary."
    )

    limits = UsageLimits(request_limit=settings.subtask_request_limit)
    try:
        result = await agent.run(user_prompt, usage_limits=limits)
    except UsageLimitExceeded as exc:
        log.warning(
            "subtask %r exceeded its %d-request budget: %s",
            name,
            settings.subtask_request_limit,
            exc,
        )
        return (
            f"subtask incomplete: budget cap reached after "
            f"{settings.subtask_request_limit} requests. "
            "Coordinator should narrow the next subtask's scope or "
            "split this work across two subtasks."
        )
    except Exception as exc:  # noqa: BLE001 — surface errors to parent
        log.exception("subtask %r failed", name)
        return f"subtask failed: {type(exc).__name__}: {exc}"
    finally:
        _safe_close(agent)

    return result.output or "(sub-agent returned empty summary)"


def make_spawn_subtask_tool(settings: Settings, repo_dir: Path):
    """Build the ``spawn_subtask`` tool exposed to the implement
    coordinator. Returns a callable the agent invokes by name."""

    async def spawn_subtask(
        name: str,
        prompt: str,
        files_in_scope: list[str] | None = None,
    ) -> str:
        """Delegate one atomic chunk of work to a sub-agent.

        Use when the ticket decomposes into multiple independent edits
        (per-file moves, per-module migrations, per-test additions)
        and you want to keep your own context lean.

        Each sub-agent runs with its own ~30-request budget so a slow
        subtask can't drain yours. The sub-agent shares your
        workspace clone, so its edits land directly on the same
        branch.

        Args:
            name: Short kebab-case label (e.g. "migrate-runners-module").
                Lands in logs.
            prompt: A self-contained mini-spec for the sub-agent —
                what files to touch, what changes to make, what
                done looks like.
            files_in_scope: Optional list of paths the sub-agent
                should focus on. Hint only; the sub-agent still has
                the full fs tool palette.

        Returns:
            The sub-agent's 1-3 sentence summary. On budget cap or
            failure, a "subtask incomplete: …" string you can act on.
        """
        return await run_spawn_subtask(
            settings=settings,
            repo_dir=repo_dir,
            name=name,
            prompt=prompt,
            files_in_scope=files_in_scope,
        )

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="spawn_subtask",
            description=(
                "Delegate ONE atomic chunk of work to a sub-agent with its "
                "own ~30-request budget and the same fs/explore/run_command "
                "tools you have. Use when the ticket decomposes into many "
                "independent edits (per-file moves, per-module migrations, "
                "per-test additions) — keeps your own context lean and the "
                "per-chunk budget bounded. Returns the sub-agent's summary."
            ),
            category="exploration",
            parameters={
                "name": "str (short kebab-case label for this subtask)",
                "prompt": "str (self-contained mini-spec for the sub-agent)",
                "files_in_scope": (
                    "list[str] (optional: paths the sub-agent should focus on)"
                ),
            },
        )
    )

    return spawn_subtask
