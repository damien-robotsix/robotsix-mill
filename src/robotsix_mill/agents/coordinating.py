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

import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, model_validator

from ..config import Settings

log = logging.getLogger(__name__)


class ImplementResult(BaseModel):
    """Structured output from the implement (coordinator) agent."""

    summary: str
    updated_memory: str = ""
    reference_files: list[str] = []

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
    reference_files: list[dict] | None = None,
    message_history: list | None = None,
    previous_attempt_summary: str | None = None,
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

    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close
    from .explore import make_explore_tool
    from .fs_tools import build_fs_tools
    from .retry import call_with_retry

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent / "agent_definitions" / "implement.yaml"
    )

    # Pre-seed fs_tools cache and build synthetic message_history when
    # reference files are provided (first invocation only, not a retry).
    pre_seeded: dict[str, str] | None = None
    final_message_history: list | None = message_history

    if reference_files and message_history is None:
        # Build pre_seeded mapping for _file_cache seeding (resolved Paths).
        # Read fresh from disk every time — the artifact is paths-only.
        pre_seeded = {}
        for rf in reference_files:
            file_path = repo_dir / rf["path"]
            try:
                pre_seeded[file_path.resolve()] = file_path.read_text(
                    encoding="utf-8", errors="replace",
                )
            except OSError:
                log.warning(
                    "reference_files: %s not found on disk, skipping",
                    rf["path"],
                )

    extra_roots: list[Path] | None = None

    fs = build_fs_tools(repo_dir, settings, pre_seeded=pre_seeded,
                        extra_roots=extra_roots)
    # the main agent reads + writes itself and includes run_command for
    # focused diagnosis (re-run a single failing test, run a linter,
    # inspect git diff, etc.). The full suite is run by the stage.
    fs_tools = [
        t for t in fs if t.__name__ in
        ("read_file", "write_file", "list_dir", "edit_file", "delete_file", "run_command")
    ]

    # Build synthetic message_history when reference files are provided
    # and the caller hasn't supplied an explicit message_history.
    # NOTE: do NOT inject a TextPart-wrapped system prompt as the first
    # message — TextPart is only valid in ModelResponse.parts. Placing
    # it in a ModelRequest triggers pydantic-ai's "Expected code to be
    # unreachable" assertion and aborts the entire implement run. The
    # system prompt is already added by build_agent below; the synthetic
    # history starts directly with the preloaded read_file ToolCall /
    # ToolReturn pairs, which pydantic-ai accepts.
    if reference_files and message_history is None:
        from pydantic_ai.messages import (
            ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart,
        )
        synthetic: list = []
        for rf in reference_files:
            file_path = repo_dir / rf["path"]
            try:
                content = file_path.read_text(
                    encoding="utf-8", errors="replace",
                )
            except OSError:
                log.warning(
                    "reference_files: %s not found on disk, "
                    "omitting from synthetic history",
                    rf["path"],
                )
                continue
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
                    content=content,
                    tool_call_id=tc_id,
                )
            ]))
        final_message_history = synthetic

    overrides = {}
    if model_name is not None:
        overrides["model_name"] = model_name
    elif not definition.model:
        overrides["model_name"] = settings.model

    agent = build_agent_from_definition(
        settings, definition,
        tools=[
            make_explore_tool(settings, repo_dir, extra_roots=extra_roots),
            *fs_tools,
        ],
        **overrides,
    )
    try:
        limits = UsageLimits(request_limit=settings.coordinator_request_limit)
        user_prompt = ""
        if epic_context:
            user_prompt += f"{epic_context}\n\n"
        user_prompt += (
            f"<ticket_spec>\n{spec}\n</ticket_spec>\n\n"
            f"<memory>\n{memory or '(empty — start a new ledger)'}\n</memory>"
        )
        if feedback:
            if previous_attempt_summary:
                # Inject prior summary before the feedback block so the
                # model doesn't undo its prior correct work.
                user_prompt = (
                    "<previous_attempt>\n"
                    "Your previous edit pass produced this summary "
                    "(already on disk):\n"
                    f"{previous_attempt_summary}\n"
                    "</previous_attempt>\n\n"
                ) + user_prompt
            if feedback.startswith("[REVIEW"):
                # Review feedback — prepend to the spec so the coordinator
                # addresses the flagged issues first.
                user_prompt = (
                    "<review_feedback>\n"
                    "The code review flagged issues. Address these review "
                    "comments before proceeding.\n"
                    "For each comment you fully address, call "
                    "`close_thread(comment_id)` to mark it resolved. If you "
                    "need to explain your approach or ask a clarifying "
                    "question, call `reply_to_thread(thread_id, body)` first.\n"
                    f"{feedback}\n"
                    "</review_feedback>\n\n"
                ) + user_prompt
            elif feedback.startswith("[SCOPE"):
                user_prompt += (
                    "\n\n<scope_violation>\n"
                    "Your previous edit pass is already on disk, but it "
                    "modified files outside the ticket's stated scope. "
                    "The ticket spec is the source of truth for what is "
                    "in scope.\n"
                    f"{feedback}\n"
                    "</scope_violation>\n\n"
                    "Revert the out-of-scope changes and stop."
                )
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
