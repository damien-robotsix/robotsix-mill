"""The implement agent.

A capable model that reads and edits the repo ITSELF to satisfy ONE
ticket. Each invocation is a single explore→read→edit pass — the
implement *stage* owns the deterministic test→retry→escalate loop and
re-invokes this agent with a distilled failure diagnosis when the suite
fails. No separate implement sub-agent — that layer just re-explored
everything and never converged.

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
    """Deterministic routing decision for one implement iteration.

    Produced by the implement *stage* after each test-gate run (NOT by
    the model). It is the single routing authority — it decides whether
    to deliver (``proceed``), re-invoke the coordinator (``retry``), or
    block the ticket (``escalate``).
    """

    passed: bool
    next_action: Literal["proceed", "retry", "escalate"]
    failure_summary: str = ""
    iterations_used: int = 0

    @classmethod
    def decide(
        cls,
        *,
        passed: bool,
        iterations: int,
        max_iters: int,
        feedback: str = "",
    ) -> "ValidationResult":
        """Route deterministically from a test-gate outcome.

        ``passed`` → ``proceed``; a failure with attempts remaining →
        ``retry``; a failure on the last allowed attempt → ``escalate``.
        No LLM is involved — for any ``(passed, iterations, max_iters)``
        triple the result is fixed.
        """
        if passed:
            next_action: Literal["proceed", "retry", "escalate"] = "proceed"
        elif iterations < max_iters:
            next_action = "retry"
        else:
            next_action = "escalate"
        return cls(
            passed=passed,
            next_action=next_action,
            failure_summary="" if passed else feedback,
            iterations_used=iterations,
        )


_SYSTEM_PROMPT = """\
You are a senior engineer implementing ONE ticket in a git repo.

Procedure:
1. `explore` to orient; `read_file` the specific files you'll change.
2. Make the smallest change that fully satisfies the spec (prefer
   `edit_file` over `write_file`); add/adjust tests for the behaviour.
3. Use `run_command` for focused checks while you work — run a single
   test, a linter, inspect `git diff`.
4. When the change is complete, stop and reply with a 1-3 sentence
   summary of what you did.

The full test suite is run for you automatically once you stop. You do
NOT run the whole suite yourself, and you do NOT decide when to give
up — that routing is handled deterministically outside this
conversation. If the suite fails you will be invoked again with a
short diagnosis inside a `<test_failure>` block: fix exactly that and
stop.

Keep your context lean: prefer `explore` over wide reading; never
paste whole files into your reasoning. Do not commit/push/touch git.

## File Content

The tool layer maintains the authoritative current content of every
file. Re-reading a file that is unchanged since your last read returns
a short stub ("already in context above — unchanged"), not a duplicate.
After you `edit_file`, the latest content is automatically available;
you never need to re-read a file immediately after editing it.

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
    def run_tests() -> str:
        """Run the project's test suite (isolated sandbox) via the test
        sub-agent. Returns 'PASS' or 'FAIL' followed by a short,
        actionable diagnosis — never the raw log."""
        from .testing import run_test_agent

        passed, feedback = run_test_agent(
            settings=settings, repo_dir=repo_dir
        )
        return f"{'PASS' if passed else 'FAIL'}: {feedback}"

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(ToolInfo(
        name="run_tests",
        description="Run the project's test suite (isolated sandbox) via the test sub-agent.",
        category="testing",
        parameters={},
    ))

    return run_tests


def run_coordinator(
    *,
    settings: Settings,
    repo_dir: Path,
    spec: str,
    memory: str = "",
    model_name: str | None = None,
    feedback: str | None = None,
    epic_context: str = "",
    epic_workspace_path: Path | None = None,
    reference_files: list[dict] | None = None,
    message_history: list | None = None,
) -> ImplementResult:
    """Run ONE explore→read→edit pass for the ticket and return the
    structured result.

    The implement *stage* owns the deterministic test→retry→escalate
    loop; when it re-invokes after a failed test gate it passes
    ``feedback`` — a distilled diagnosis of the previous run's failure —
    which is appended to the prompt as a ``<test_failure>`` block. The
    partial edits from earlier passes persist on disk in ``repo_dir``,
    so a retry continues from the current working tree. The seam tests
    monkeypatch this."""
    from pydantic_ai import PromptedOutput
    from pydantic_ai.usage import UsageLimits

    from .base import build_agent, _safe_close
    from .explore import make_explore_tool
    from .fs_tools import build_fs_tools
    from .retry import call_with_retry

    # Pre-seed fs_tools cache and build synthetic message_history when
    # reference files are provided (first invocation only, not a retry).
    pre_seeded: dict[str, str] | None = None
    final_message_history: list | None = message_history

    if reference_files and message_history is None:
        # Build pre_seeded mapping for _file_cache seeding (resolved Paths).
        pre_seeded = {
            (repo_dir / rf["path"]).resolve(): rf["content"]
            for rf in reference_files
        }

    extra_roots: list[Path] = (
        [epic_workspace_path] if epic_workspace_path is not None else None
    )

    fs = build_fs_tools(repo_dir, settings, pre_seeded=pre_seeded,
                        extra_roots=extra_roots)
    # the main agent reads + writes itself and includes run_command for
    # focused diagnosis (re-run a single failing test, run a linter,
    # inspect git diff, etc.). The full suite is run by the stage.
    fs_tools = [
        t for t in fs if t.__name__ in
        ("read_file", "write_file", "list_dir", "edit_file", "delete_file", "run_command")
    ]

    # Build synthetic message_history on first pass (no feedback set).
    # NOTE: do NOT inject a TextPart-wrapped system prompt as the first
    # message — TextPart is only valid in ModelResponse.parts. Placing
    # it in a ModelRequest triggers pydantic-ai's "Expected code to be
    # unreachable" assertion and aborts the entire implement run. The
    # system prompt is already added by build_agent below; the synthetic
    # history starts directly with the preloaded read_file ToolCall /
    # ToolReturn pairs, which pydantic-ai accepts.
    if reference_files and message_history is None and feedback is None:
        from pydantic_ai.messages import (
            ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart,
        )
        synthetic: list = []
        for rf in reference_files:
            tc_id = f"preload_{rf['path']}"
            synthetic.append(ModelResponse(parts=[
                ToolCallPart(
                    tool_name="read_file",
                    args={"path": rf["path"], "offset": 1, "limit": None},
                    tool_call_id=tc_id,
                )
            ]))
            synthetic.append(ModelRequest(parts=[
                ToolReturnPart(
                    tool_name="read_file",
                    content=rf["content"],
                    tool_call_id=tc_id,
                )
            ]))
        final_message_history = synthetic

    agent = build_agent(
        settings,
        system_prompt=_SYSTEM_PROMPT,
        output_type=PromptedOutput(ImplementResult),
        tools=[
            make_explore_tool(settings, repo_dir, extra_roots=extra_roots),
            *fs_tools,
        ],
        web=True,  # adds the cheap web_research tool
        model_name=model_name if model_name is not None else settings.model,  # the capable implement model
        name="implement",
    )
    try:
        limits = UsageLimits(request_limit=settings.coordinator_request_limit)
        user_prompt = ""
        if epic_context:
            user_prompt += f"{epic_context}\n\n"
        if epic_workspace_path is not None:
            user_prompt += (
                "You can read and write files under `_epic/...` to share "
                "reference documents with other child tickets of this epic. "
                "The epic workspace persists across tickets; the repo clone "
                "does not.\n\n"
            )
        user_prompt += (
            f"<ticket_spec>\n{spec}\n</ticket_spec>\n\n"
            f"<memory>\n{memory or '(empty — start a new ledger)'}\n</memory>"
        )
        if feedback:
            if feedback.startswith("[REVIEW"):
                # Review feedback — prepend to the spec so the coordinator
                # addresses the flagged issues first.
                user_prompt = (
                    "<review_feedback>\n"
                    "The code review flagged issues. Address these review "
                    "comments before proceeding:\n"
                    f"{feedback}\n"
                    "</review_feedback>\n\n"
                ) + user_prompt
            else:
                user_prompt += (
                    "\n\n<test_failure>\n"
                    "Your previous edit pass is already on disk, but the test "
                    "suite then failed. Diagnosis:\n"
                    f"{feedback}\n"
                    "</test_failure>\n\n"
                    "Fix exactly this failure and stop."
                )
        result = call_with_retry(
            lambda: agent.run_sync(
                user_prompt,
                message_history=final_message_history,
                usage_limits=limits,
            ),
            settings=settings, what="implement",
        )
    finally:
        _safe_close(agent)
    return result.output
