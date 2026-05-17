"""The implement agent: an LLM that edits a cloned repo via sandboxed
tools until the change is done.

This is the single seam the stage drives — tests monkeypatch
``run_implement_agent`` to avoid network/LLM. ``history`` is the running
pydantic-ai message list; persisting it (``dump_history``/
``load_history``) is what lets a BLOCKED ticket *resume* its reasoning
instead of restarting from scratch.

Budget/agent failures raise :class:`AgentBudgetError` /
:class:`AgentRunError` carrying the partial transcript, so the stage can
commit WIP, save the transcript, and block-as-resumable.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Settings
from .fs_tools import build_fs_tools

SYSTEM_PROMPT = """\
You are a senior software engineer working inside a single git repository.
All file and shell tools operate inside that repo only.

Start by exploring with list_dir — do not assume paths. Spec paths are
usually relative to the package, not the repo root (e.g. a src/ layout),
so locate files before reading or editing them.

You may be RESUMING earlier work: inspect the repo (it may already have
partial changes) before acting, and continue rather than restart.

Implement the ticket below end to end:
- Make the smallest change that fully satisfies it, matching the
  surrounding code's style and conventions.
- Add or update tests for the behaviour you change.
- You may run shell commands (tests, linters, build) to verify yourself.
- Do not commit, push, or touch git — the system handles that.

When you are confident the change is complete and tests pass, stop and
reply with a short summary of what you changed and why.
"""


class AgentBudgetError(RuntimeError):
    """Usage/budget cap hit mid-run — operationally retryable."""

    def __init__(self, message: str, messages: list) -> None:
        super().__init__(message)
        self.messages = messages


class AgentRunError(RuntimeError):
    """Agent raised mid-run — block-as-resumable, keep the transcript."""

    def __init__(self, message: str, messages: list) -> None:
        super().__init__(message)
        self.messages = messages


def run_implement_agent(
    *,
    settings: Settings,
    repo_dir: Path,
    spec: str,
    feedback: str | None = None,
    history: list | None = None,
) -> tuple[str, list]:
    """Run one implementation pass. Returns ``(summary, messages)``.
    On a usage cap or agent error, raises Agent*Error carrying the
    partial messages so the caller can persist + resume."""
    from pydantic_ai import capture_run_messages
    from pydantic_ai.exceptions import UsageLimitExceeded
    from pydantic_ai.usage import UsageLimits

    from .base import build_agent

    tools = build_fs_tools(repo_dir, settings)
    agent = build_agent(
        settings, system_prompt=SYSTEM_PROMPT, tools=tools, web=True
    )

    if feedback is None:
        prompt = f"<ticket>\n{spec}\n</ticket>"
    else:
        prompt = (
            "The verification command still fails. Diagnose and fix it.\n\n"
            f"<test_output>\n{feedback}\n</test_output>"
        )

    limits = UsageLimits(request_limit=settings.agent_request_limit)
    # capture_run_messages keeps the transcript even when the run raises
    with capture_run_messages() as msgs:
        try:
            result = agent.run_sync(
                prompt, message_history=history, usage_limits=limits
            )
        except UsageLimitExceeded as e:
            raise AgentBudgetError(str(e), list(msgs)) from e
        except Exception as e:  # noqa: BLE001 — block-as-resumable, keep msgs
            raise AgentRunError(str(e), list(msgs)) from e
    return str(result.output), result.all_messages()


def dump_history(messages: list) -> bytes:
    """Serialize a pydantic-ai message list for resume."""
    from pydantic_ai.messages import ModelMessagesTypeAdapter

    return ModelMessagesTypeAdapter.dump_json(messages)


def load_history(data: bytes) -> list:
    from pydantic_ai.messages import ModelMessagesTypeAdapter

    return list(ModelMessagesTypeAdapter.validate_json(data))
