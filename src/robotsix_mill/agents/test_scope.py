"""Cheap test-scope classifier that decides whether a change needs the
full test suite.

Mirrors :mod:`scope_triage`: a level-1 structured-output agent (no tools)
loaded from ``agent_definitions/test_scope.yaml`` and invoked via
:func:`~.yaml_loader.load_and_run_agent`.

The agent may only ever *reduce* trust in a skip — it can VETO a skip the
deterministic extension check would otherwise grant (e.g. a
behaviour-affecting ``.json``/``.yaml``), and it fails safe to
``needs_full_suite=True`` on any error, so the full deterministic suite is
always the final backstop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from ..config import Settings


class TestScopeVerdict(BaseModel):
    """Classifier verdict on whether the change needs the full test suite.

    ``needs_full_suite`` is ``False`` ONLY when the diff cannot affect
    runtime behaviour (documentation, comments, and inert configuration
    that no code path reads at runtime).  ``rationale`` is the
    human-readable justification.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    needs_full_suite: bool
    rationale: str


def run_test_scope_agent(
    *,
    settings: "Settings",
    changed_files: list[str],
    diff_stat: str,
    ticket_summary: str,
) -> TestScopeVerdict:
    """Decide whether the change needs the full test suite.

    Builds a cheap no-tools classifier from the ``test_scope`` YAML
    definition and runs it (with retry) over the changed file list,
    ``git diff --stat`` output, and ticket intent, returning a
    structured :class:`TestScopeVerdict`.

    Args:
        settings: Application configuration.
        changed_files: List of paths changed relative to the target
            branch (from ``git diff --name-only --diff-filter=ACMR``).
        diff_stat: ``git diff --stat`` output summarising the diff.
        ticket_summary: Short description of the ticket intent (e.g.
            the ticket title or spec summary).

    Returns:
        A :class:`TestScopeVerdict`.  Fails safe to
        ``needs_full_suite=True`` when the API key is missing or the
        model call raises — the full deterministic suite is always the
        final backstop.
    """
    from ..config import get_secrets

    from .yaml_loader import load_and_run_agent

    changed_body = "\n".join(f"- {f}" for f in changed_files)
    diff_body = diff_stat if diff_stat else "(no diff available)"
    user_prompt = (
        "## Changed files\n\n"
        + changed_body
        + "\n\n"
        + "## Diff stat\n\n"
        + "```\n"
        + diff_body
        + "\n```\n\n"
        + "## Ticket intent\n\n"
        + ticket_summary
    )

    if not get_secrets().openrouter_api_key:
        return TestScopeVerdict(
            needs_full_suite=True,
            rationale="no API key configured — defaulting to full suite",
        )

    try:
        result = load_and_run_agent(
            settings=settings,
            definition_name="test_scope",
            tools=[],
            prompt=user_prompt,
            what="test-scope",
        )
    except Exception as exc:
        return TestScopeVerdict(
            needs_full_suite=True,
            rationale=f"agent call failed ({type(exc).__name__}: {exc}) — defaulting to full suite",
        )
    output = result.output
    if not isinstance(output, TestScopeVerdict):
        raise TypeError(f"Expected TestScopeVerdict, got {type(output).__name__}")
    return output
