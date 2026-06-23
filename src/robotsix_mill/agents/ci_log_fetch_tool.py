"""The ``fetch_ci_logs`` tool — lets the ci-fix agent fetch CI job logs.

The ci-fix agent receives a failure summary that includes truncated job logs
keyed by run id.  When it needs the full log for a specific run — or when a
truncated window doesn't show enough context — it calls ``fetch_ci_logs``
with the run id (or a run URL) and receives the log contents.

The tool runs HOST-SIDE (like ``wait_for_ci`` and the bridged git tools): the
agent stays inside its ``--network none`` sandbox and never reaches the forge
directly.  All forge access is funnelled through the ``fetch_fn`` closure
supplied by the stage.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from ..runtime.tracing import trace_stage

# A fetch_fn returns the log text for a given run_id.  When full_log is
# False, the forge returns size-capped, failure-window-anchored logs;
# True returns the complete (still ANSI-stripped) job logs.
CiLogFetchFn = Callable[[int, bool], str]

# Regex to extract a run id from a GitHub Actions run URL, e.g.:
#   https://github.com/owner/repo/actions/runs/12345/...
_RUN_URL_RE = re.compile(r"/runs/(\d+)")


def build_ci_log_fetch_tool(
    *,
    branch: str,
    fetch_fn: CiLogFetchFn | None,
) -> Callable[..., Any]:
    """Build the ``fetch_ci_logs`` tool closure.

    The returned callable has a type-hinted signature + docstring so
    pydantic-ai can derive its JSON schema.

    *branch* is used only for the guardrailed-branch note in the docstring
    (the forge's own auth scoping enforces access).  *fetch_fn* is the
    host-side forge probe that returns log text for a given run id.

    When *fetch_fn* is ``None`` (e.g. the multi-repo merge path, which
    runs its own external re-check loop), the tool is still wired so the
    prompt's call directive resolves, but every call returns
    ``CI_LOG_FETCH_UNAVAILABLE``.
    """

    def fetch_ci_logs(
        run_id: int = 0,
        run_url: str = "",
        full_log: bool = False,
    ) -> str:
        """Fetch CI job logs for a workflow run.

        Call with either *run_id* (the numeric GitHub Actions / GitLab CI run
        id printed in the failure summary) or *run_url* (the full run URL, e.g.
        ``https://github.com/owner/repo/actions/runs/12345``).

        When *full_log* is ``False`` (default), returns the log tail anchored on
        the first failure marker — enough to diagnose most failures.  Set
        *full_log=True* for the complete logs when the truncated window doesn't
        show enough context.

        Returns the log text with ``### Job: <name> (id=...)`` headers
        separating each failed job, or a clear error string when the run has no
        failed jobs / the fetch fails.

        Guardrailed branch: {branch}
        """.format(branch=branch)

        with trace_stage("fetch_ci_logs"):
            if fetch_fn is None:
                return (
                    "CI_LOG_FETCH_UNAVAILABLE: log fetching is not wired in this "
                    "context. The failure summary already includes a truncated log "
                    "tail — use that to diagnose the failure."
                )

            # Resolve run id: explicit run_id takes precedence; fall back to
            # parsing a run URL.
            resolved: int | None = None
            if run_id:
                resolved = run_id
            elif run_url:
                m = _RUN_URL_RE.search(run_url)
                if m:
                    resolved = int(m.group(1))

            if resolved is None:
                return (
                    "error: fetch_ci_logs requires either a run_id (int) or a "
                    "run_url (str) containing a /runs/<id> path segment."
                )

            if resolved <= 0:
                return f"error: invalid run_id={resolved}"

            try:
                logs = fetch_fn(resolved, full_log)
            except Exception as exc:
                return (
                    f"error: log fetch failed for run {resolved}: "
                    f"{type(exc).__name__}: {exc}"
                )

            if not logs:
                return (
                    f"(run {resolved} has no failed jobs — all jobs in this run "
                    f"passed or the run was cancelled before any job ran)"
                )

            return logs

    return fetch_ci_logs


# Register in the system-wide capability catalog so the prompt-tool-consistency
# guard and smoke tests recognise the tool name.
from .tool_registry import ToolInfo, ToolRegistry  # noqa: E402

ToolRegistry.register(
    ToolInfo(
        name="fetch_ci_logs",
        description=(
            "Fetch CI job logs for a workflow run by run id or run URL. "
            "Returns failure-window-anchored logs by default; pass "
            "full_log=True for the complete logs. Each failed job is "
            "separated by a ``### Job: ...`` header."
        ),
        category="git",
        parameters={
            "run_id": "int",
            "run_url": "str",
            "full_log": "bool",
        },
    )
)
