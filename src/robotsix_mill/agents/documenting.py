"""Documentation agent: classifies diff impact and updates docs.

The agent reads the ticket spec + git diff, classifies the change as
user-facing or internal-only, and — for user-facing changes — surveys
the repo's existing docs and applies targeted surgical edits.

Returns a structured ``DocResult`` with ``user_facing`` and ``summary``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..config import Settings


class DocResult(BaseModel):
    """Structured output from the documentation agent."""

    user_facing: bool = Field(
        description="True when the diff introduces a user-facing change "
                    "(new feature, API change, config key, CLI flag, "
                    "behavioral change a user would notice). False for "
                    "internal-only changes (refactor, bug-fix with no doc "
                    "impact, test/CI-only, lint/format)."
    )
    summary: str = Field(
        min_length=1,
        description="Summary of documentation changes made, or a note "
                    "that no changes were needed.",
    )


SYSTEM_PROMPT = """\
You are a senior technical writer responsible for keeping this repository's
documentation accurate and up-to-date. You receive a ticket specification
and the corresponding git diff.

Your job has two steps:

### Step 1 — Classify the change
Determine whether this diff is **user-facing** or **internal-only**.

**User-facing**: a new feature, API change, new config key, new CLI flag,
or any behavioral change a user would notice. Even small user-facing
changes (e.g. a new default, a renamed option) count.

**Internal-only**: a pure refactor, a bug fix with no documentation
impact, test/CI-only changes, lint/format changes, or dependency bumps
that don't change user-visible behavior.

### Step 2 — Act on the classification

**If internal-only**: return immediately with `user_facing=False` and a
summary of "no user-facing changes (internal-only)". Do NOT write any
files or make any edits.

**If user-facing**:
1. Use `explore` to understand the repo's documentation structure
   (what's in README.md, docs/, AGENT.md).
2. Use `list_dir`, `read_file` to survey the existing docs.
3. Decide which docs need updating to reflect the change.
4. Apply **minimal, surgical edits**:
   - Use `edit_file` with `old_string`/`new_string` for small targeted
     changes to existing files.
   - Use `write_file` only for new files or full rewrites.
5. Follow the existing documentation conventions in the repo — do NOT
   invent new structure or reorganize existing docs.
6. After editing, return `user_facing=True` and a concise summary of
   what was changed (e.g. "updated README.md with new config key").
"""


def run_doc_agent(
    *,
    settings: Settings,
    repo_dir,
    diff: str,
    spec: str,
    model_name: str | None = None,
) -> DocResult:
    """Build a documentation agent, classify *diff* + *spec*, and update
    docs for user-facing changes.

    The agent receives the ticket spec and git diff. It surveys the
    repo's docs (README.md, docs/*, AGENT.md) and applies targeted
    edits for user-facing changes. Internal-only changes are a no-op."""
    from pydantic_ai import PromptedOutput
    from pydantic_ai.usage import UsageLimits

    from .base import build_agent, _safe_close
    from .explore import make_explore_tool
    from .fs_tools import build_fs_tools
    from .retry import call_with_retry

    fs = build_fs_tools(repo_dir, settings)
    agent = build_agent(
        settings,
        system_prompt=SYSTEM_PROMPT,
        output_type=PromptedOutput(DocResult),
        tools=[
            make_explore_tool(settings, repo_dir),
            *(t for t in fs if t.__name__ in ("read_file", "write_file", "list_dir", "edit_file")),
        ],
        web=False,
        report_issue=False,
        model_name=model_name if model_name is not None else settings.doc_model,
        name="document",
    )
    try:
        user_prompt = (
            f"<ticket_spec>\n{spec}\n</ticket_spec>\n\n"
            f"<git_diff>\n{diff}\n</git_diff>"
        )
        limits = UsageLimits(request_limit=4)
        result = call_with_retry(
            lambda: agent.run_sync(user_prompt, usage_limits=limits),
            settings=settings, what="document",
        )
        return result.output
    finally:
        _safe_close(agent)
