"""The implement agent: a CHEAP driver model that orchestrates, and
delegates the heavy lifting to context-isolated sub-agents.

The driver (``settings.model``) has a limited context window, so it
never reads the repo or runs tests directly. It only:
  - ``explore(question)``     — read the repo (fresh sub-agent context)
  - ``web_research(query)``   — look things up (cheap sub-agent)
  - ``deep_implement(ctx)``   — the STRONG model authors the change
  - ``write_file(path, c)``   — apply what deep_implement returned
  - ``run_tests()``           — verify (sandbox; trimmed result)

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

from .. import sandbox
from ..config import Settings
from .deep import make_deep_implement_tool
from .explore import make_explore_tool
from .fs_tools import build_fs_tools

SYSTEM_PROMPT = """\
You are an orchestrator running on a SMALL-context model. You do NOT
read the repo, write code, or run tests yourself — you have no tools
for that on purpose. You coordinate sub-agents and keep your own
context minimal: never paste large file contents into your reasoning;
pass them straight between tools.

Loop:
1. Use `explore` to learn what you need (structure, where things live,
   and the FULL content of every file the change will touch). Ask
   targeted questions; do not hoard — request the next thing when you
   need it. Use `web_research` for anything not in the repo.
2. Assemble a COMPLETE, self-contained context (the spec verbatim, the
   full current content of each file to change, conventions, and on a
   retry the failing test output) and pass it to `deep_implement`. The
   strong model returns a plan plus, per file, a `FILE: <path>` block
   with that file's COMPLETE final content (and maybe a `DELETE:` line).
3. Apply it exactly with `write_file` for each FILE block (you may need
   `explore` to fetch a file verbatim first so you forward it intact).
4. Call `run_tests`. If it fails, go back to step 2 with the trimmed
   failure output added to the context — do not try to fix it yourself.

You may be RESUMING: use `explore` to see existing partial changes and
continue rather than restart. Do not commit/push/touch git — the system
does that. When `run_tests` passes (or there is no test gate), stop and
reply with a 1–3 sentence summary of what changed and why.
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

    # Driver toolset only: it must delegate reading (explore) and
    # verifying (run_tests); the sole repo-mutating tool is write_file
    # (to apply what deep_implement returns). build_agent adds
    # web_research when web=True.
    write_file = next(
        t for t in build_fs_tools(repo_dir, settings)
        if t.__name__ == "write_file"
    )

    def run_tests() -> str:
        """Run the project's test command in the isolated sandbox and
        return a trimmed pass/fail result (full logs are NOT returned —
        keep your context small; the tail is enough to diagnose)."""
        cmd = settings.test_command.strip()
        if not cmd:
            return "no test gate configured (treat as passing)"
        try:
            rc, out = sandbox.run(cmd, repo_dir=repo_dir, settings=settings)
        except sandbox.SandboxError as e:
            return f"sandbox unavailable: {e}"
        tail = out[-3000:]
        return f"{'PASS' if rc == 0 else 'FAIL'} (rc={rc})\n{tail}"

    tools = [
        make_explore_tool(settings, repo_dir),
        make_deep_implement_tool(settings),
        write_file,
        run_tests,
    ]
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
    from .retry import call_with_retry

    with capture_run_messages() as msgs:
        try:
            result = call_with_retry(
                lambda: agent.run_sync(
                    prompt, message_history=history, usage_limits=limits
                ),
                settings=settings, what="implement",
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
