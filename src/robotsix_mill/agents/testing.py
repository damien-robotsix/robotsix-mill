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

from ..config import RepoConfig, Settings, get_secrets


def run_test_agent(
    *,
    settings: Settings,
    repo_dir: Path,
    repo_config: RepoConfig | None = None,
) -> tuple[bool, str]:
    """Run the test command in the sandbox. Return ``(passed,
    feedback)``. On pass, feedback is a short confirmation; on fail it
    is a cheap-model distilled, actionable diagnosis (NOT the raw log).
    Sandbox infra failure -> ``(False, "<reason>")`` so the coordinator
    can react.

    Test command resolution: ``repo_config.test_command`` wins when
    set (the multi-repo authoritative source), else
    ``settings.test_command`` (legacy / single-repo). When both are
    empty the gate short-circuits to PASS — repos without a test
    suite (doc-only, etc.) need no opt-out flag."""
    from .. import sandbox

    cmd = (
        (repo_config.test_command if repo_config else "") or settings.test_command
    ).strip()
    if not cmd:
        return True, "no test gate configured (treated as passing)"
    try:
        rc, out = sandbox.run(cmd, repo_dir=repo_dir, settings=settings)
    except sandbox.SandboxError as e:
        return False, f"sandbox unavailable: {e}"
    if rc == 0:
        return True, "all tests passed"

    tail = out[-6000:]
    if not get_secrets().openrouter_api_key:
        return False, f"tests failed (rc={rc}); raw tail:\n{tail[-1500:]}"

    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close
    from .explore import make_explore_tool
    from .fs_tools import build_fs_tools
    from .retry import call_with_retry

    from pydantic_ai.usage import UsageLimits

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent / "agent_definitions" / "tester.yaml"
    )

    all_fs = build_fs_tools(repo_dir, settings)
    ro_fs_tools = [
        t for t in all_fs if t.__name__ in ("read_file", "list_dir", "run_command")
    ]
    explore_tool = make_explore_tool(settings, repo_dir)

    agent = build_agent_from_definition(
        settings,
        definition,
        tools=[*ro_fs_tools, explore_tool],
        model_name=definition.model or settings.test_model,
    )
    limits = UsageLimits(request_limit=settings.test_request_limit)
    try:
        result = call_with_retry(
            lambda: agent.run_sync(
                f"<test_output rc={rc}>\n{tail}\n</test_output>",
                usage_limits=limits,
            ),
            settings=settings,
            what="test-distill",
        )
        return False, str(result.output).strip()
    except Exception as e:  # noqa: BLE001 — degrade to raw tail
        return False, f"tests failed (rc={rc}); distill error {e}:\n{tail[-1500:]}"
    finally:
        _safe_close(agent)
