"""The ``wait_for_ci`` tool — lets the CI-fix agent own its own fix→verify loop.

The ci-fix agent fixes a failure, pushes, then calls ``wait_for_ci`` to block
until the latest CI run on its branch finishes and returns the verdict.  On a
fresh failure the agent fixes again and re-calls; on green it reports DONE.

This collapses what used to be an external state-machine loop (FIXING_CI ⇄
IMPLEMENT_COMPLETE re-polling, gated by attempt/cycle/fingerprint counters) into
a single bounded tool: the iteration cap lives in the tool's own call counter,
so the agent stays in charge until CI is green or the budget is spent.

The tool runs HOST-SIDE (like the bridged git tools): the agent stays inside its
``--network none`` sandbox and never reaches the forge directly.  All forge
access is funnelled through the ``ci_status_fn`` closure supplied by the stage.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ..runtime.tracing import trace_stage

# A ci_status_fn returns ``(conclusion, failing_summary)`` where conclusion is
# one of:
#   "success" — every check is green (failing_summary unused, "")
#   "failure" — at least one check failed (failing_summary describes it)
#   "pending" — checks not yet complete (keep waiting)
#   "gone"    — the PR/branch is no longer visible on the forge
CiStatusFn = Callable[[], "tuple[str, str]"]


def build_ci_wait_tool(
    *,
    branch: str,
    ci_status_fn: CiStatusFn | None,
    max_iterations: int = 5,
    poll_interval_s: float = 30.0,
    timeout_s: float = 1500.0,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Callable[..., Any]:
    """Build the ``wait_for_ci`` tool closure.

    The returned callable has a type-hinted signature + docstring so
    pydantic-ai can derive its JSON schema.

    *branch* guardrails the tool to the ticket's own branch.  *ci_status_fn*
    is the host-side forge probe.  *max_iterations* caps how many times the
    agent may wait-and-recheck (each call counts one); past the cap the tool
    refuses and tells the agent to report FAILED.  Each call polls every
    *poll_interval_s* seconds up to *timeout_s* before returning a
    still-pending signal.  *sleep* / *monotonic* are injectable for tests.

    When *ci_status_fn* is ``None`` (e.g. the multi-repo merge path, which
    runs its own external re-check loop), the tool is still wired so the
    prompt's call directive resolves, but every call returns
    ``CI_VERIFICATION_UNAVAILABLE`` — the agent should push its fix and report
    DONE, and the caller re-checks CI.
    """
    # Mutable call counter captured in the closure (the iteration budget).
    state = {"calls": 0}

    def wait_for_ci(branch_name: str) -> str:
        """Block until the latest CI run on the ticket branch finishes, then
        return its verdict. Call this AFTER you have committed AND pushed a fix
        (via git_push_with_lease) — it waits for the freshly-triggered run.

        Possible return values (match on the leading token):

        - ``CI_PASSED`` — every check is green. Report DONE.
        - ``CI_FAILING (attempt N/M): <summary>`` — CI is still red; the new
          failing summary follows. Fix it and push again, then call
          wait_for_ci once more.
        - ``CI_STILL_PENDING`` — checks did not finish within the wait window.
          Call wait_for_ci again to keep waiting, or report FAILED if CI looks
          stuck.
        - ``CI_ITERATION_CAP_REACHED`` — you have used your entire CI
          verification budget. Stop and report FAILED with what remains broken.
        - ``CI_GONE`` — the PR/branch vanished from the forge. Report FAILED.

        Guardrailed: only the ticket's own branch is accepted."""
        with trace_stage("wait_for_ci"):
            if branch_name != branch:
                return (
                    f"error: wait_for_ci is guardrailed to ticket branch "
                    f"'{branch}' — '{branch_name}' rejected"
                )

            if ci_status_fn is None:
                return (
                    "CI_VERIFICATION_UNAVAILABLE: CI verification is not wired in "
                    "this context. Push your fix (git_push_with_lease) and report "
                    "DONE — the orchestrator will re-check CI."
                )

            state["calls"] += 1
            if state["calls"] > max_iterations:
                return (
                    f"CI_ITERATION_CAP_REACHED: you have used all {max_iterations} "
                    f"CI verification attempt(s) for this ticket. Stop fixing and "
                    f"report FAILED with a brief summary of what is still broken."
                )

            attempt = state["calls"]
            deadline = monotonic() + timeout_s
            while True:
                conclusion, summary = ci_status_fn()
                if conclusion == "success":
                    sha_note = f" ({summary})" if summary else ""
                    return f"CI_PASSED: all checks are green{sha_note} — report DONE."
                if conclusion == "failure":
                    return (
                        f"CI_FAILING (attempt {attempt}/{max_iterations}):\n\n{summary}"
                    )
                if conclusion == "gone":
                    return (
                        "CI_GONE: the PR/branch is no longer visible on the forge "
                        "— report FAILED."
                    )
                # conclusion == "pending" (or anything unexpected) — keep waiting.
                if monotonic() >= deadline:
                    return (
                        f"CI_STILL_PENDING: checks have not finished after "
                        f"{int(timeout_s)}s (attempt {attempt}/{max_iterations}). "
                        f"Call wait_for_ci again to keep waiting, or report FAILED "
                        f"if CI appears stuck."
                    )
                sleep(poll_interval_s)

    return wait_for_ci


# Register in the system-wide capability catalog so the prompt-tool-consistency
# guard and smoke tests recognise the tool name.
from .tool_registry import ToolInfo, ToolRegistry  # noqa: E402

ToolRegistry.register(
    ToolInfo(
        name="wait_for_ci",
        description=(
            "Block until the latest CI run on the ticket branch finishes and "
            "return its verdict (CI_PASSED / CI_FAILING / CI_STILL_PENDING / "
            "CI_ITERATION_CAP_REACHED / CI_GONE). Call after pushing a fix. "
            "Iteration-capped and guardrailed to the ticket branch."
        ),
        category="git",
        parameters={"branch_name": "str"},
    )
)
