"""The implement agent.

A capable model that reads and edits the repo ITSELF — but keeps its
context lean via two cheap sub-agents:

- ``explore`` — a scout that returns concise pointers (paths, symbols,
  line ranges), NEVER whole files. The main agent then reads only the
  specific files it needs with ``read_file``.
- ``run_tests`` — the test sub-agent runs the suite in the sandbox and
  returns a distilled PASS/FAIL diagnosis, never the raw log.

It implements directly (``read_file``/``write_file``/``edit_file``/``list_dir``),
runs tests, and loops on failure. No separate implement sub-agent —
that layer just re-explored everything and never converged.

``run_coordinator`` is the seam ``coding.run_implement_agent`` drives
(name kept for the stage/tests).
"""

from __future__ import annotations

from pathlib import Path

from ..config import Settings

_SYSTEM_PROMPT = """\
You are a senior engineer implementing ONE ticket in a git repo.

Tools:
- `explore(question)` — a fast scout: it returns the paths/symbols/
  line-ranges relevant to your question, NOT file contents. Use it to
  locate things instead of scanning the tree yourself.
- `read_file`/`list_dir` — read exactly the files explore pointed you
  to (only what you need; don't bulk-read).
- `edit_file(path, old_string, new_string)` — replace a unique string
  in a file; **prefer this for changes**.
- `write_file` — create a new file, or overwrite when `edit_file`
  reports it can't apply.
- `web_research(query)` — anything not in the repo.
- `run_tests()` — runs the suite in the sandbox; returns PASS or FAIL
  plus a short, actionable diagnosis (never the raw log).

Procedure:
1. `explore` to orient; `read_file` the specific files you'll change.
2. Make the smallest change that fully satisfies the spec (prefer
   `edit_file` over `write_file`); add/adjust tests for the behaviour.
3. `run_tests`. On PASS, stop and reply with a 1–3 sentence summary.
4. On FAIL, fix using the diagnosis and `run_tests` again — at most
   {max_iters} test cycles; if still failing, stop and reply starting
   with "UNRESOLVED:" and a short reason.

Keep your context lean: prefer `explore` over wide reading; never
paste whole files into your reasoning. Do not commit/push/touch git.
"""


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
    """Drive explore → read → implement → test → loop. Returns the
    final summary (starts with 'UNRESOLVED:' if it gave up). The seam
    tests monkeypatch this."""
    from pydantic_ai.usage import UsageLimits

    from .base import build_agent
    from .explore import make_explore_tool
    from .fs_tools import build_fs_tools
    from .retry import call_with_retry

    fs = build_fs_tools(repo_dir, settings)
    # the main agent reads + writes itself; tests go through the test
    # sub-agent (run_tests), so no raw run_command here.
    fs_tools = [
        t for t in fs if t.__name__ in
        ("read_file", "write_file", "list_dir", "edit_file")
    ]
    agent = build_agent(
        settings,
        system_prompt=_SYSTEM_PROMPT.format(
            max_iters=settings.max_fix_iterations
        ),
        tools=[
            make_explore_tool(settings, repo_dir),
            *fs_tools,
            make_run_tests_tool(settings, repo_dir),
        ],
        web=True,  # adds the cheap web_research tool
        model_name=settings.model,  # the capable implement model
        name="implement",
    )
    limits = UsageLimits(request_limit=settings.coordinator_request_limit)
    result = call_with_retry(
        lambda: agent.run_sync(
            f"<ticket_spec>\n{spec}\n</ticket_spec>", usage_limits=limits
        ),
        settings=settings, what="implement",
    )
    return str(result.output).strip()
