"""Documentation agent: classifies diff impact and updates docs.

The agent reads the ticket spec + git diff, classifies the change as
user-facing or internal-only, and — for user-facing changes — surveys
the repo's existing docs and applies targeted surgical edits.

Returns a structured ``DocResult`` with ``user_facing`` and ``summary``.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from ..config import Settings


class DocClassifierResult(BaseModel):
    """Cheap classification from the doc-classifier agent.

    Determines whether a diff is user-facing or internal-only without
    any tools — pure diff-and-spec inspection.  The only real risk is a
    wrong "internal-only" that skips needed docs, so the agent is
    instructed to bias conservative (user-facing when uncertain).
    """

    user_facing: bool = Field(
        description="True when the diff introduces a user-facing change "
                    "(new feature, API change, config key, CLI flag, "
                    "behavioral change a user would notice). False for "
                    "internal-only changes (refactor, bug-fix with no doc "
                    "impact, test/CI-only, lint/format)."
    )
    classification: str = Field(
        min_length=1,
        description="One-line human-readable classification, e.g. "
                    "'internal-only — model field rename' or "
                    "'user-facing — new CLI flag'.",
    )


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


def run_doc_agent(
    *,
    settings: Settings,
    repo_dir,
    diff: str,
    spec: str,
    model_name: str | None = None,
    extra_roots: list[Path] | None = None,
) -> DocResult:
    """Build a documentation agent, classify *diff* + *spec*, and update
    docs for user-facing changes.

    The agent receives the ticket spec and git diff. It surveys the
    repo's docs (README.md, docs/*, AGENT.md) and applies targeted
    edits for user-facing changes. Internal-only changes are a no-op."""
    from pydantic_ai.usage import UsageLimits

    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close
    from .explore import make_explore_tool
    from .fs_tools import build_fs_tools
    from .retry import call_with_retry

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent / "agent_definitions" / "document.yaml"
    )

    fs = build_fs_tools(repo_dir, settings, extra_roots=extra_roots)
    overrides = {}
    if model_name is not None:
        overrides["model_name"] = model_name
    elif not definition.model:
        overrides["model_name"] = settings.doc_model
    agent = build_agent_from_definition(
        settings, definition,
        tools=[
            make_explore_tool(settings, repo_dir, extra_roots=extra_roots),
            *(t for t in fs if t.__name__ in ("read_file", "write_file", "list_dir", "edit_file")),
        ],
        **overrides,
    )
    try:
        user_prompt = (
            f"<ticket_spec>\n{spec}\n</ticket_spec>\n\n"
            f"<git_diff>\n{diff}\n</git_diff>"
        )
        limits = UsageLimits(request_limit=settings.doc_request_limit)
        result = call_with_retry(
            lambda: agent.run_sync(user_prompt, usage_limits=limits),
            settings=settings, what="document",
        )
        return result.output
    finally:
        _safe_close(agent)


def run_doc_classifier(
    *,
    settings: Settings,
    diff: str,
    spec: str,
    model_name: str | None = None,
) -> DocClassifierResult:
    """Run a cheap, tool-free classifier to decide whether *diff* is
    user-facing or internal-only.

    The classifier receives only the ticket spec and git diff — no
    ``explore``, no file-system tools.  It biases conservative:
    "user-facing" when uncertain.  Designed to run in <15s for <$0.005
    on internal-only diffs, so the full (expensive) document agent can
    be skipped entirely for those changes.
    """
    from pydantic_ai.usage import UsageLimits

    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close
    from .retry import call_with_retry

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent / "agent_definitions" / "doc_classifier.yaml"
    )

    agent = build_agent_from_definition(
        settings, definition,
        tools=[],
        model_name=model_name or definition.model or settings.doc_classifier_model,
    )

    user_prompt = (
        f"<ticket_spec>\n{spec}\n</ticket_spec>\n\n"
        f"<git_diff>\n{diff}\n</git_diff>"
    )

    try:
        result = call_with_retry(
            lambda: agent.run_sync(
                user_prompt,
                usage_limits=UsageLimits(request_limit=settings.doc_classifier_request_limit),
            ),
            settings=settings,
            what="doc classifier",
        )
        return result.output
    finally:
        _safe_close(agent)
