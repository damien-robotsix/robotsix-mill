"""The test sub-agent.

Runs the project's test command in the isolated sandbox (mechanical,
deterministic), then — on failure — a CHEAP model distills the raw
output into a short, actionable diagnosis the coordinator can turn
into the next precise implement instruction. The coordinator never
sees the full log; its history stays short.

``run_test_agent`` is the mockable seam.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Settings

_SYSTEM_PROMPT = """\
You are given the raw output of a failing test run. Produce a SHORT,
actionable diagnosis for the engineer who will fix it:
- which test(s) failed and the essential error/assertion,
- the most likely cause and the file(s) to change,
- nothing else — no preamble, no full tracebacks, <=12 lines.
"""


def run_test_agent(
    *,
    settings: Settings,
    repo_dir: Path,
    epic_workspace_path: Path | None = None,
) -> tuple[bool, str]:
    """Run the test command in the sandbox. Return ``(passed,
    feedback)``. On pass, feedback is a short confirmation; on fail it
    is a cheap-model distilled, actionable diagnosis (NOT the raw log).
    Sandbox infra failure -> ``(False, "<reason>")`` so the coordinator
    can react."""
    from .. import sandbox

    cmd = settings.test_command.strip()
    if not cmd:
        return True, "no test gate configured (treated as passing)"
    try:
        rc, out = sandbox.run(cmd, repo_dir=repo_dir, settings=settings,
                              epic_workspace_path=epic_workspace_path)
    except sandbox.SandboxError as e:
        return False, f"sandbox unavailable: {e}"
    if rc == 0:
        return True, "all tests passed"

    tail = out[-6000:]
    if not settings.openrouter_api_key:
        return False, f"tests failed (rc={rc}); raw tail:\n{tail[-1500:]}"

    from pydantic_ai.usage import UsageLimits

    from .base import build_agent, _safe_close
    from .explore import make_explore_tool
    from .fs_tools import build_fs_tools
    from .retry import call_with_retry

    all_fs = build_fs_tools(repo_dir, settings)
    ro_fs_tools = [
        t for t in all_fs
        if t.__name__ in ("read_file", "list_dir", "run_command")
    ]
    explore_tool = make_explore_tool(settings, repo_dir)

    agent = build_agent(
        settings,
        system_prompt=_SYSTEM_PROMPT,
        model_name=settings.test_model,
        name="run_tests",
        tools=[*ro_fs_tools, explore_tool],
    )
    limits = UsageLimits(request_limit=settings.test_request_limit)
    try:
        result = call_with_retry(
            lambda: agent.run_sync(
                f"<test_output rc={rc}>\n{tail}\n</test_output>",
                usage_limits=limits,
            ),
            settings=settings, what="test-distill",
        )
        return False, str(result.output).strip()
    except Exception as e:  # noqa: BLE001 — degrade to raw tail
        return False, f"tests failed (rc={rc}); distill error {e}:\n{tail[-1500:]}"
    finally:
        _safe_close(agent)
