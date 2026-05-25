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


class DocClassifierResult(BaseModel):
    """Output from the doc classifier — a cheap pre-gate that decides
    whether a change needs the full (expensive) document agent."""

    user_facing: bool = Field(
        description="True for user-facing changes that need documentation "
                    "(new feature, API change, config key, CLI flag, "
                    "behavioral change). False for internal-only changes "
                    "(refactor, test/CI-only, lint/format, model renames)."
    )
    classification: str = Field(
        min_length=1,
        description="One-line human-readable classification, e.g. "
                    "'internal-only — model field rename' or "
                    "'user-facing — new CLI flag'.",
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
) -> DocClassifierResult:
    """Cheap classifier gate: inspect the diff + spec and decide whether
    the change is user-facing (needs full doc agent) or internal-only
    (can skip it).

    This follows the same pattern as ``triage_refine()``: loads the
    no-tool ``doc_classifier.yaml`` definition, builds an agent with
    zero tools, and makes a single cheap LLM call.  Conservative bias:
    when uncertain, classify as user-facing so the full agent runs
    (the only real risk is a wrong "internal-only" that skips needed
    docs).
    """
    from pydantic_ai.usage import UsageLimits

    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close
    from .retry import call_with_retry

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent
        / "agent_definitions"
        / "doc_classifier.yaml"
    )
    agent = build_agent_from_definition(
        settings,
        definition,
        tools=[],
        model_name=definition.model or settings.doc_classifier_model,
    )
    try:
        user_prompt = (
            f"<ticket_spec>\n{spec}\n</ticket_spec>\n\n"
            f"<git_diff>\n{diff}\n</git_diff>"
        )
        limits = UsageLimits(request_limit=settings.doc_classifier_request_limit)
        result = call_with_retry(
            lambda: agent.run_sync(user_prompt, usage_limits=limits),
            settings=settings,
            what="doc classifier",
        )
        return result.output
    finally:
        _safe_close(agent)
