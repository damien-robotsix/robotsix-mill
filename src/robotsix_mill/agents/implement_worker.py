"""The implement sub-agent.

The coordinator explores + plans, then delegates the actual code change
here with PRECISE instructions. This agent is **stateless** — a fresh
context every fix iteration (the coordinator passes the latest precise
instructions, including any test-failure feedback). Its large editing
context dies with the call; only a concise summary returns, so the
coordinator's own history stays short.

It has file tools only (no shell, no tests, no git) — the coordinator
owns running tests and the loop. ``run_implement_worker`` is the
mockable seam.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Settings

_SYSTEM_PROMPT = """\
You are a senior engineer making a SINGLE, well-specified code change.
You are given precise instructions (and possibly a failing-test report
to fix). Do exactly what is asked — no more, no less.

- Use list_dir/read_file to inspect, write_file to edit. Match the
  surrounding code's style and conventions.
- Add or update tests for the behaviour you change when the
  instructions call for it.
- Do NOT run shell commands, tests, or git — that is handled for you.
- When done, stop and reply with a 1–3 sentence summary of exactly
  what you changed (files + the essence). No preamble.
"""


def run_implement_worker(
    *,
    settings: Settings,
    repo_dir: Path,
    instructions: str,
) -> str:
    """Apply one precise change set. Returns a concise summary. Fresh
    context each call (no history) — the coordinator re-invokes with
    refined instructions on the next fix iteration."""
    from pydantic_ai.usage import UsageLimits

    from .base import build_agent
    from .fs_tools import build_fs_tools

    fs = build_fs_tools(repo_dir, settings)
    tools = [t for t in fs if t.__name__ in
             ("read_file", "write_file", "list_dir")]
    agent = build_agent(
        settings,
        system_prompt=_SYSTEM_PROMPT,
        tools=tools,
        model_name=settings.implement_model,
    )
    from .retry import call_with_retry

    limits = UsageLimits(request_limit=settings.agent_request_limit)
    result = call_with_retry(
        lambda: agent.run_sync(instructions, usage_limits=limits),
        settings=settings, what="implement-worker",
    )
    return str(result.output).strip()
