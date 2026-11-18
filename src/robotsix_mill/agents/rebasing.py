"""Rebase agent: resolves merge conflicts on a stale PR branch.

Runs ``git rebase origin/<target>`` inside the
ticket's existing workspace clone. On conflict it reads the conflicted
files, edits them in place, and continues the rebase.

The agent now DRIVES the full push flow via bridged git tools
(``git_fetch``, ``git_remote_sha``, ``git_push_with_lease``,
``git_branch_ancestry``) that the mill executes host-side with
the per-repo token — the agent stays network-isolated and never
sees credentials.  On a lease rejection the agent inspects the
remote ancestry and auto-recovers when the remote only carries
its own prior rebase push (no foreign commits).

Also hosts the rebase-supersede judge (:func:`run_supersede_check`):
when the post-rebase integrity check reports implement-stage files
absent from the diff, this LLM compares the ticket spec against the
target's actual content to separate genuine drops from changes the
target already satisfies.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel

from ..config import Settings, get_secrets
from .prompt_blocks import section


class RebaseResult(BaseModel):
    """Structured output from the rebase agent."""

    status: Literal["DONE", "FAILED"]
    summary: str
    updated_memory: str = ""


#: Char cap per file when embedding origin/<target> content into the
#: supersede-check prompt — the judge needs enough to compare the spec
#: against the target's version, not a full dump of a giant file.
_SUPERSEDE_EMBED_MAX_CHARS = 4000


class SupersedeVerdict(BaseModel):
    """Whether origin/<target> already satisfies the spec for one file."""

    path: str
    superseded: bool = False
    reason: str = ""


class SupersedeResult(BaseModel):
    """Structured output from the rebase-supersede judge."""

    verdicts: list[SupersedeVerdict] = []
    summary: str = ""


def run_supersede_check(
    *,
    settings: Settings,
    repo_dir: Path,
    target: str,
    spec: str,
    dropped_files: list[str],
    target_contents: dict[str, str],
) -> SupersedeResult:
    """Decide which of *dropped_files* origin/<target> already satisfies.

    The deterministic post-rebase integrity check compares bytes, not
    meaning: it reports an implement-stage file as dropped whenever the
    target lacks the branch's exact blob, even when the target already
    implements the spec's requirement for that file through different
    content (e.g. a CI job that migrated into a shared reusable
    workflow). This LLM judgement compares the ticket spec against the
    target's actual content (captured host-side with
    ``git show origin/<target>:<path>``) and returns a per-file verdict.

    This is the mockable seam — tests monkeypatch it to avoid real LLM
    calls.
    """
    if not get_secrets().openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    from .yaml_loader import load_and_run_agent

    blocks: list[str] = []
    for path in dropped_files:
        content = target_contents.get(path, "")
        heading = f"origin/{target}:{path}"
        if not content:
            blocks.append(
                f"### `{path}`\n\n**{heading}** — the file does not exist on "
                f"origin/{target}, so origin/{target} cannot already satisfy "
                "the spec for it. Verdict: superseded=false.\n"
            )
            continue
        if len(content) > _SUPERSEDE_EMBED_MAX_CHARS:
            content = (
                content[:_SUPERSEDE_EMBED_MAX_CHARS]
                + f"\n…[truncated at {_SUPERSEDE_EMBED_MAX_CHARS} chars]"
            )
        blocks.append(
            f"### `{path}`\n\n**{heading}** (content on origin/{target}):\n"
            f"```\n{content}\n```\n"
        )

    user_prompt = (
        "A rebase collapsed the implement-stage changes to the files below, "
        "and the deterministic post-rebase check cannot tell whether that is "
        "a genuine loss or the target branch already delivering the change "
        "(it compares bytes, not meaning). For each file, compare the ticket "
        "spec's required change for that file against the file's actual "
        f"content on origin/{target} shown below.\n\n"
        "## Ticket spec (the planned change)\n\n"
        f"{spec}\n\n"
        "## Files absent from the post-rebase diff\n\n"
        + "\n".join(blocks)
        + "\nReturn a verdict for every file: superseded=true when "
        f"origin/{target} already satisfies the spec for that file, "
        "superseded=false otherwise, with a one-line reason."
    )

    result = load_and_run_agent(
        settings=settings,
        definition_name="rebase_supersede",
        tools=[],
        prompt=user_prompt,
        what="rebase supersede check",
        repo_dir=Path(repo_dir),
        system_prompt_format_kwargs={"repo_dir": repo_dir, "target": target},
    )

    return cast(SupersedeResult, result.output)


def run_rebase_agent(
    *,
    settings: Settings,
    repo_dir: Path,
    branch: str,
    target: str,
    memory: str = "",
    remote_url: str | None = None,
    token: str | None = None,
    token_provider: Callable[[], str | None] | None = None,
    token_cache_clear: Callable[[], None] | None = None,
    pre_rebase_files: list[str] | None = None,
    previously_dropped_files: list[str] | None = None,
) -> RebaseResult:
    """Run one rebase attempt of *branch* onto ``origin/<target>``.

    Uses the LLM (pydantic-ai agent) with sandboxed file + shell tools
    scoped to *repo_dir*, plus bridged git tools that execute host-side
    with *remote_url* and *token* so the agent can drive fetch + push.

    The agent loops internally to resolve every conflict the rebase
    encounters; it returns a ``RebaseResult`` with status, summary,
    and updated memory.

    This is the mockable seam — tests monkeypatch it to avoid real LLM
    and Docker calls.
    """
    if not get_secrets().openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    from .bridged_git_tools import build_bridged_git_tools
    from .fs_tools import build_fs_tools
    from .yaml_loader import load_and_run_agent

    # Build sandboxed fs tools confined to the ticket's own clone.
    tools = build_fs_tools(Path(repo_dir), settings)

    # Build bridged git tools (host-side, with per-repo token) so the
    # agent can drive fetch + push without ever seeing credentials.
    # Always built — when remote_url is None (e.g. tests), the tools
    # return clear errors rather than failing silently.
    tools.extend(
        build_bridged_git_tools(
            repo_dir=Path(repo_dir),
            branch=branch,
            target=target,
            remote_url=remote_url or "",
            token=token,
            token_provider=token_provider,
            token_cache_clear=token_cache_clear,
        )
    )

    user_prompt = (
        f"Rebase branch '{branch}' onto origin/{target} in {repo_dir}. "
        + "Follow the system prompt exactly.\n\n"
    )

    if pre_rebase_files:
        shown = "\n".join(f"  - {p}" for p in pre_rebase_files)
        user_prompt += (
            f"## Implement-stage files (MUST survive the rebase)\n\n"
            f"These files were introduced or modified by the PR's implement "
            f"stage. Every one of them must still exist and contain the PR's "
            f"changes after the rebase completes. If a conflict resolution "
            f"would delete or empty one of these files, you MUST re-apply the "
            f"PR-side change — do NOT accept a resolution that discards the "
            f"PR's work.\n\n"
            f"{shown}\n\n"
        )

    if previously_dropped_files:
        shown = "\n".join(f"  - {p}" for p in previously_dropped_files)
        user_prompt += (
            f"## Previously dropped files (DO NOT drop again)\n\n"
            f"The previous rebase attempt silently dropped these files. "
            f"They MUST be preserved in this attempt. If git would delete "
            f"them during conflict resolution, re-apply the PR's changes "
            f"instead.\n\n"
            f"{shown}\n\n"
        )

    user_prompt += section("memory", memory or "(empty — start a new ledger)")

    result = load_and_run_agent(
        settings=settings,
        definition_name="rebase",
        tools=tools,
        prompt=user_prompt,
        what="rebase",
        repo_dir=Path(repo_dir),
        system_prompt_format_kwargs={
            "repo_dir": repo_dir,
            "target": target,
            "branch": branch,
        },
    )

    return result.output
