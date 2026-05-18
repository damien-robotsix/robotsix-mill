"""The implement coordinator.

A capable model that ORCHESTRATES but never holds raw files or logs —
so its history stays short. It:

1. uses ``explore`` (cheap sub-agent) to understand the repo,
2. uses ``web_research`` (cheap sub-agent) for anything external,
3. drafts a concrete plan,
4. delegates the edit to ``implement(instructions)`` — the capable
   implement sub-agent, FRESH each call, given precise instructions,
5. calls ``run_tests`` — the cheap test sub-agent runs the suite and
   returns a distilled diagnosis (never the raw log),
6. loops 4–5 with refined instructions until tests pass or the fix
   budget is exhausted.

``run_coordinator`` is the seam ``coding.run_implement_agent`` drives.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Settings

_SYSTEM_PROMPT = """\
You are an implementation COORDINATOR. You do not read the repo, write
code, or run tests yourself — you have no tools for that on purpose.
Keep your own context minimal: never paste file contents or test logs
into your reasoning; pass concise instructions and act on concise
results.

Procedure:
1. Use `explore` to learn the structure and the exact current content
   of the files the change touches. Ask targeted questions; use
   `web_research` for anything not in the repo.
2. Draft a concrete plan: the precise edits, file by file, plus the
   tests to add/update.
3. Call `implement` with PRECISE, self-contained instructions for the
   whole change (it is stateless — include everything it needs:
   target files, exact changes, the relevant current code, the tests
   to write). Do NOT make it explore.
4. Call `run_tests`. If it returns PASS, stop and reply with a 1–3
   sentence summary of the change.
5. If it returns FAIL, call `implement` again with NEW precise
   instructions that incorporate the distilled failure (re-`explore`
   only if you genuinely need fresher context). Repeat at most
   {max_iters} implement→test cycles; if still failing, stop and
   reply starting with "UNRESOLVED:" and a short reason.

Do not commit, push, or touch git — the system handles that.
"""


def make_implement_tool(settings: Settings, repo_dir: Path):
    def implement(instructions: str) -> str:
        """Delegate the code change to the implement sub-agent with
        PRECISE, self-contained instructions (it is fresh/stateless and
        cannot explore — include the target files, exact edits, the
        relevant current code, and the tests to write). Returns a
        concise summary of what it changed."""
        from .implement_worker import run_implement_worker

        return run_implement_worker(
            settings=settings, repo_dir=repo_dir, instructions=instructions
        )

    return implement


def make_run_tests_tool(settings: Settings, repo_dir: Path):
    def run_tests() -> str:
        """Run the project's test suite (isolated sandbox) via the test
        sub-agent. Returns 'PASS' or 'FAIL' followed by a short,
        actionable diagnosis — never the raw log."""
        from .testing import run_test_agent

        passed, feedback = run_test_agent(
            settings=settings, repo_dir=repo_dir
        )
        return f"{'PASS' if passed else 'FAIL'}: {feedback}"

    return run_tests


def run_coordinator(
    *,
    settings: Settings,
    repo_dir: Path,
    spec: str,
) -> str:
    """Drive explore → plan → implement → test → loop. Returns the
    coordinator's final summary (starts with 'UNRESOLVED:' if it gave
    up). The seam tests monkeypatch this."""
    from pydantic_ai.usage import UsageLimits

    from .base import build_agent
    from .explore import make_explore_tool
    from .retry import call_with_retry

    agent = build_agent(
        settings,
        system_prompt=_SYSTEM_PROMPT.format(
            max_iters=settings.max_fix_iterations
        ),
        tools=[
            make_explore_tool(settings, repo_dir),
            make_implement_tool(settings, repo_dir),
            make_run_tests_tool(settings, repo_dir),
        ],
        web=True,  # adds the cheap web_research tool
        model_name=settings.model,  # the capable coordinator model
    )
    limits = UsageLimits(request_limit=settings.coordinator_request_limit)
    result = call_with_retry(
        lambda: agent.run_sync(
            f"<ticket_spec>\n{spec}\n</ticket_spec>", usage_limits=limits
        ),
        settings=settings, what="coordinator",
    )
    return str(result.output).strip()
