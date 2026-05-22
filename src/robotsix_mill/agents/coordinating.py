"""The implement agent.

A capable model that reads and edits the repo ITSELF. It implements
directly, runs tests, and loops on failure. No separate implement
sub-agent — that layer just re-explored everything and never converged.

``run_coordinator`` is the seam ``coding.run_implement_agent`` drives
(name kept for the stage/tests).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, model_validator

from ..config import Settings


class ImplementResult(BaseModel):
    """Structured output from the implement (coordinator) agent."""

    summary: str
    updated_memory: str = ""

    @model_validator(mode="before")
    @classmethod
    def _absorb_summary_typos(cls, data):
        """deepseek-v4-pro repeatedly mis-keys the required ``summary``
        field. pydantic-ai's strict validation then exceeds output
        retries, the implement stage blocks the ticket with "Exceeded
        maximum output retries", and the user pays $1+ in coordinator
        cost per retry.

        Two-tier absorption:
        1. Preferred: a known near-miss key (``summary_text``, ``text``,
           ``result``, etc.).
        2. Fallback: any non-``updated_memory`` string value in the
           dict — the schema has only two string fields, so anything
           else the model emitted under a different name is almost
           certainly the intended summary.

        Only kicks in when canonical ``summary`` is missing/empty —
        correctly-keyed output passes straight through. Empty values
        are NOT absorbed (a genuinely-empty summary still surfaces
        downstream).
        """
        if not isinstance(data, dict):
            return data
        if data.get("summary"):
            return data
        # Tier 1: known near-misses in priority order.
        for k in ("summary_text", "summary_str", "summaryText",
                  "result_summary", "text", "result", "output"):
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                data["summary"] = v
                return data
        # Tier 2: any non-updated_memory string value. Pick the
        # longest — heuristically the most likely candidate for a
        # multi-sentence summary.
        candidates = [
            (k, v) for k, v in data.items()
            if k not in ("summary", "updated_memory")
            and isinstance(v, str) and v.strip()
        ]
        if candidates:
            best_k, best_v = max(candidates, key=lambda kv: len(kv[1]))
            data["summary"] = best_v
        return data


class ValidationResult(BaseModel):
    """Deterministic routing result from the ``run_tests`` tool."""

    passed: bool
    next_action: Literal["proceed", "retry", "escalate"]
    failure_summary: str = ""
    iterations_used: int = 0


_SYSTEM_PROMPT = """\
You are a senior engineer implementing ONE ticket in a git repo.

Procedure:
1. `explore` to orient; `read_file` the specific files you'll change.
2. Make the smallest change that fully satisfies the spec (prefer
   `edit_file` over `write_file`); add/adjust tests for the behaviour.
3. `run_tests`. Returns a structured result with fields:
   - `passed`: boolean
   - `next_action`: one of `"proceed"`, `"retry"`, `"escalate"`
   - `failure_summary`: short diagnosis if failed
   - `iterations_used`: how many test cycles so far (max {max_iters})
4. On `next_action == "proceed"`: stop and reply with a 1–3 sentence summary.
5. On `next_action == "retry"`: use `run_command` to narrow the problem —
   re-run just the failing test, check with a linter, inspect `git diff` —
   then fix and `run_tests` again.
6. On `next_action == "escalate"`: STOP immediately. Do NOT attempt
   another fix. Reply starting with "UNRESOLVED:" and a short reason
   incorporating the `failure_summary`.

Keep your context lean: prefer `explore` over wide reading; never
paste whole files into your reasoning. Do not commit/push/touch git.

## Memory

You are given a `<memory>` block containing a Markdown ledger of
observations from your past runs in this deployment. It records:
- Repo architecture and file-layout conventions
- Testing patterns and build-system quirks
- Notable gotchas and successful strategies

Reference the memory to avoid re-discovering what you already know.
After the run, update the memory in your `updated_memory` field:
- Add new architecture/file-layout observations
- Record any gotcha you encountered
- Note successful strategies that saved time
- Keep entries concise and ticket-ID-qualified (e.g. "Observed in
  `<ticket-id>`: ...") so the ledger stays coherent across runs
- If nothing new was learned, return the incoming memory unchanged
"""


def make_run_tests_tool(settings: Settings, repo_dir: Path):
    max_iters = settings.max_fix_iterations
    iterations = 0

    def run_tests() -> ValidationResult:
        """Run the project's test suite (isolated sandbox) via the test
        sub-agent. Returns a ValidationResult with deterministic routing."""
        nonlocal iterations
        iterations += 1
        from .testing import run_test_agent

        passed, feedback = run_test_agent(
            settings=settings, repo_dir=repo_dir
        )

        if passed:
            next_action = "proceed"
        elif iterations <= max_iters:
            next_action = "retry"
        else:
            next_action = "escalate"

        return ValidationResult(
            passed=passed,
            next_action=next_action,
            failure_summary=feedback if not passed else "",
            iterations_used=iterations,
        )

    return run_tests


def run_coordinator(
    *,
    settings: Settings,
    repo_dir: Path,
    spec: str,
    memory: str = "",
    model_name: str | None = None,
) -> ImplementResult:
    """Drive explore → read → implement → test → loop. Returns the
    structured result. The seam tests monkeypatch this."""
    from pydantic_ai import PromptedOutput
    from pydantic_ai.usage import UsageLimits

    from .base import build_agent, _safe_close
    from .explore import make_explore_tool
    from .fs_tools import build_fs_tools
    from .retry import call_with_retry

    fs = build_fs_tools(repo_dir, settings)
    # the main agent reads + writes itself and includes run_command
    # for focused diagnosis between run_tests cycles (re-run a single
    # failing test, run a linter, inspect git diff, etc.).
    fs_tools = [
        t for t in fs if t.__name__ in
        ("read_file", "write_file", "list_dir", "edit_file", "delete_file", "run_command")
    ]
    agent = build_agent(
        settings,
        system_prompt=_SYSTEM_PROMPT.format(
            max_iters=settings.max_fix_iterations
        ),
        output_type=PromptedOutput(ImplementResult),
        tools=[
            make_explore_tool(settings, repo_dir),
            *fs_tools,
            make_run_tests_tool(settings, repo_dir),
        ],
        web=True,  # adds the cheap web_research tool
        model_name=model_name if model_name is not None else settings.model,  # the capable implement model
        name="implement",
    )
    try:
        limits = UsageLimits(request_limit=settings.coordinator_request_limit)
        user_prompt = (
            f"<ticket_spec>\n{spec}\n</ticket_spec>\n\n"
            f"<memory>\n{memory or '(empty — start a new ledger)'}\n</memory>"
        )
        result = call_with_retry(
            lambda: agent.run_sync(user_prompt, usage_limits=limits),
            settings=settings, what="implement",
        )
    finally:
        _safe_close(agent)
    return result.output
