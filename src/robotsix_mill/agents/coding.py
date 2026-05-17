"""The implement agent: an LLM that edits a cloned repo via sandboxed
tools until the change is done.

This is the single seam the stage drives — tests monkeypatch
``run_implement_agent`` to avoid network/LLM. The function is stateless
across the fix loop except for ``history`` (the running message list),
which lets a retry see what it already tried.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Settings
from .fs_tools import build_fs_tools

SYSTEM_PROMPT = """\
You are a senior software engineer working inside a single git repository.
All file and shell tools operate inside that repo only.

Implement the ticket below end to end:
- Make the smallest change that fully satisfies it, matching the
  surrounding code's style and conventions.
- Add or update tests for the behaviour you change.
- You may run shell commands (tests, linters, build) to verify yourself.
- Do not commit, push, or touch git — the system handles that.

When you are confident the change is complete and tests pass, stop and
reply with a short summary of what you changed and why.
"""


def run_implement_agent(
    *,
    settings: Settings,
    repo_dir: Path,
    spec: str,
    feedback: str | None = None,
    history: list | None = None,
) -> tuple[str, list]:
    """Run one implementation pass. Returns ``(summary, messages)``;
    pass ``messages`` back as ``history`` on a retry so the agent
    remembers prior attempts."""
    from .base import build_agent

    tools = build_fs_tools(repo_dir, settings)
    agent = build_agent(settings, system_prompt=SYSTEM_PROMPT, tools=tools)

    if feedback is None:
        prompt = f"<ticket>\n{spec}\n</ticket>"
    else:
        prompt = (
            "The verification command still fails. Diagnose and fix it.\n\n"
            f"<test_output>\n{feedback}\n</test_output>"
        )

    result = agent.run_sync(prompt, message_history=history)
    return str(result.output), result.all_messages()
