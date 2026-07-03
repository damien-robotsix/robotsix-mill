"""CI-fix agent: auto-fixes failing remote CI checks on a PR branch.

Reads the failing check-run summary/details from the forge, inspects
the affected files in the ticket's workspace clone, makes the minimal
code change to fix the failure, runs the project's local tests, and
commits.

The agent now DRIVES the full push flow via bridged git tools
(``git_fetch``, ``git_remote_sha``, ``git_push_with_lease``,
``git_branch_ancestry``) that the mill executes host-side with
the per-repo token — the agent stays network-isolated and never
sees credentials.  On a lease rejection the agent inspects the
remote ancestry and auto-recovers when the remote only carries
its own prior push (no foreign commits).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ..config import Settings, get_secrets
from .prompt_blocks import section


class CiFixResult(BaseModel):
    """Structured output from the CI-fix agent."""

    model_config = ConfigDict(strict=True, extra="forbid")

    status: Literal["DONE", "FAILED", "OUT_OF_SCOPE"]
    summary: str
    updated_memory: str = ""
    pattern_category: str = ""
    pattern_signature: str = ""
    pattern_approach: str = ""
    out_of_scope_reason: str = ""
    failing_check: str = ""
    required_change_area: str = ""


def run_ci_fix_agent(
    *,
    settings: Settings,
    repo_dir: Path,
    branch: str,
    failing_summary: str,
    memory: str = "",
    ticket_id: str = "",
    board_id: str = "",
    target: str = "main",
    remote_url: str | None = None,
    token: str | None = None,
    ci_status_fn: "Callable[[], tuple[str, str]] | None" = None,
    ci_log_fetch_fn: "Callable[[int, bool], str] | None" = None,
) -> CiFixResult:
    """Run the CI-fix agent, which OWNS the fix→push→verify loop.

    Uses the LLM (pydantic-ai agent) with sandboxed file + shell tools
    scoped to *repo_dir*, plus bridged git tools that execute host-side
    with *remote_url* and *token* so the agent can drive fetch + push.

    When *ci_status_fn* is provided, the agent also gets the ``wait_for_ci``
    tool: after pushing a fix it blocks on that tool until the latest CI run
    finishes, then either reports DONE (green) or fixes the fresh failure and
    re-checks — up to ``settings.ci_fix_max_iterations`` waits. This replaces
    the old one-shot-per-cycle model. *ci_status_fn* is the host-side forge
    probe returning ``(conclusion, failing_summary)``.

    When *ci_log_fetch_fn* is provided, the agent also gets the
    ``fetch_ci_logs`` tool: it can fetch the logs for any workflow run by
    run id or URL, with an option for full (uncapped) logs.  *ci_log_fetch_fn*
    is the host-side forge probe returning log text for a given run id and
    *full_log* flag.

    Returns a ``CiFixResult`` with status, summary, and updated memory.

    This is the mockable seam — tests monkeypatch it to avoid real LLM
    and Docker calls.
    """
    if not get_secrets().openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    from pydantic_ai.usage import UsageLimits

    from .yaml_loader import load_and_run_agent
    from .fs_tools import build_fs_tools

    # --- load structured pattern memory ---
    from .ci_patterns import (
        CiPatternEntry,
        find_relevant_patterns,
        load_patterns,
        save_patterns,
    )

    patterns_file = settings.ci_patterns_file_for(board_id)
    patterns = load_patterns(patterns_file)
    relevant = find_relevant_patterns(patterns, failing_summary, limit=3)

    if relevant:
        lines: list[str] = []
        for p in relevant:
            verdict = "SUCCESS" if p.success else "FAILED"
            lines.append(
                f"- [{verdict}, {p.attempts} attempt(s)] {p.category}: "
                f'"{p.signature}" → {p.approach} (ticket {p.ticket_id})'
            )
        patterns_text = "\n".join(lines)
    else:
        patterns_text = "(no prior patterns for this failure)"

    # Build sandboxed fs tools confined to the ticket's own clone.
    tools = build_fs_tools(Path(repo_dir), settings)

    # Build bridged git tools (host-side, with per-repo token) so the
    # agent can drive fetch + push without ever seeing credentials.
    # Always built — when remote_url is empty (e.g. tests), the tools
    # return clear errors rather than failing silently.
    from .bridged_git_tools import build_bridged_git_tools

    tools.extend(
        build_bridged_git_tools(
            repo_dir=Path(repo_dir),
            branch=branch,
            target=target,
            remote_url=remote_url or "",
            token=token,
        )
    )

    # Give the agent ownership of the fix→push→verify loop: after pushing it
    # calls wait_for_ci to block on the freshly-triggered CI run, then either
    # reports DONE (green) or fixes the new failure and re-checks. The
    # iteration budget lives inside the tool's call counter. The tool is
    # always wired so the prompt's call directive resolves; when ci_status_fn
    # is None (multi-repo merge path / tests) it returns
    # CI_VERIFICATION_UNAVAILABLE and the caller re-checks CI externally.
    from .ci_wait_tool import build_ci_wait_tool

    tools.append(
        build_ci_wait_tool(
            branch=branch,
            ci_status_fn=ci_status_fn,
            max_iterations=settings.ci_fix_max_iterations,
            poll_interval_s=settings.ci_fix_wait_poll_interval_s,
            timeout_s=settings.ci_fix_wait_timeout_s,
        )
    )

    # Give the agent a tool to fetch CI logs on demand — the failure summary
    # includes truncated logs, but the agent may need the full log for a
    # specific run or a run the summary didn't include.  The tool is always
    # wired so the prompt's call directive resolves; when ci_log_fetch_fn is
    # None (multi-repo merge path / tests) it returns
    # CI_LOG_FETCH_UNAVAILABLE.
    from .ci_log_fetch_tool import build_ci_log_fetch_tool

    tools.append(
        build_ci_log_fetch_tool(
            branch=branch,
            fetch_fn=ci_log_fetch_fn,
        )
    )

    user_prompt_parts = [
        f"CI is failing on branch '{branch}' in {repo_dir}. "
        + "Here is the failing check summary:\n\n"
        + f"```\n{failing_summary}\n```",
    ]
    if patterns_text != "(no prior patterns for this failure)":
        user_prompt_parts.append(
            "## Prior fix attempts for similar failures\n\n" + patterns_text
        )
    if memory:
        user_prompt_parts.append(section("memory", memory))
    user_prompt_parts.append("Follow the system prompt exactly.")
    user_prompt = "\n\n".join(user_prompt_parts)

    result = load_and_run_agent(
        settings=settings,
        definition_name="ci_fix",
        tools=tools,
        prompt=user_prompt,
        what="ci_fix",
        repo_dir=Path(repo_dir),
        board_id=board_id,
        system_prompt_format_kwargs={
            "repo_dir": repo_dir,
            "branch": branch,
            "target": target,
        },
        run_kwargs={
            "usage_limits": UsageLimits(request_limit=settings.ci_fix_request_limit)
        },
    )

    # --- persist structured pattern entry ---
    output = result.output
    # An OUT_OF_SCOPE verdict is not a fix-attempt pattern — skip persistence.
    if output.pattern_signature and output.status != "OUT_OF_SCOPE":
        from datetime import datetime, timezone

        entry = CiPatternEntry(
            category=output.pattern_category or "unknown",
            signature=output.pattern_signature,
            approach=output.pattern_approach or "unknown",
            success=(output.status == "DONE"),
            attempts=1,
            ticket_id=ticket_id or "unknown",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        patterns.append(entry)
        try:
            save_patterns(patterns_file, patterns)
        except Exception:
            import logging

            logging.getLogger(__name__).warning(
                "ci_fix: failed to save patterns to %s",
                patterns_file,
                exc_info=True,
            )

    import opentelemetry.trace

    try:
        opentelemetry.trace.get_tracer_provider().force_flush(timeout_millis=5000)  # type: ignore[attr-defined]
    except Exception:
        import logging

        logging.getLogger(__name__).debug("ci_fix: force_flush failed", exc_info=True)
    return output
