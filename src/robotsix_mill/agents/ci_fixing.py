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

from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from ..config import Settings, get_secrets
from .prompt_blocks import section


class CiFixResult(BaseModel):
    """Structured output from the CI-fix agent."""

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
) -> CiFixResult:
    """Run one CI-fix attempt based on *failing_summary*.

    Uses the LLM (pydantic-ai agent) with sandboxed file + shell tools
    scoped to *repo_dir*, plus bridged git tools that execute host-side
    with *remote_url* and *token* so the agent can drive fetch + push.

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

    user_prompt = (
        f"CI is failing on branch '{branch}' in {repo_dir}. "
        + "Here is the failing check summary:\n\n"
        + f"```\n{failing_summary}\n```\n\n"
        + section("memory", memory or "(empty — start a new ledger)")
        + "\n\n"
        + "Follow the system prompt exactly."
    )

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
            "patterns": patterns_text,
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
