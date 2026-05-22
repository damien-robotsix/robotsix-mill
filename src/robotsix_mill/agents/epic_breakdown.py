"""Epic breakdown agent: reads an epic description and produces a
list of well-scoped child tickets.

Seam: tests monkeypatch ``run_epic_breakdown_agent``.  The agent does
NOT get filesystem access — it only sees the epic title + description.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..config import Settings

SYSTEM_PROMPT = """\
You are a ticket-breakdown agent for an autonomous software project.
Your job is to read an epic description and produce a focused list of
concrete, self-contained child tickets.  Each child ticket should:

- Represent ONE clear, independently deliverable piece of work.
- Have a concise, actionable title (a developer should understand what
  to do from the title alone).
- Have a concrete body with scope, acceptance criteria, and any
  relevant constraints or references drawn from the epic description.
- Cover the full scope of the epic without overlap — the union of all
  children should deliver the epic's goal.

Rules:
- Break the epic into 2–8 children.  Prefer smaller, well-scoped
  tickets over a few giant ones.
- Do NOT fabricate dependencies between children.
- Do NOT assign priorities or estimate effort.
- Each child body must be self-contained — a developer picking up that
  ticket should not have to re-read the epic to understand what's
  needed.
- If the epic description is vague or underspecified, do your best
  with what's there.  Do not refuse to produce tickets.
- Titles should start with a verb when possible (e.g. "Add X",
  "Refactor Y", "Fix Z", "Document W").

Return a list of child ticket titles and a parallel list of bodies
(same length; same order).

After producing the child ticket lists, also produce an ``epic_body``
field: a revised epic description that explains the global breakdown
strategy at a high level. It should help a reviewer understand why the
epic was split into these particular children and what each child
contributes to the overall goal. Keep it concise — a short paragraph
or two.
"""


class EpicBreakdownResult(BaseModel):
    child_titles: list[str] = Field(default_factory=list)
    child_bodies: list[str] = Field(default_factory=list)
    epic_body: str | None = None


def run_epic_breakdown_agent(
    *,
    settings: Settings,
    epic_title: str,
    epic_description: str,
) -> EpicBreakdownResult:
    """Break an epic into well-scoped child tickets.

    The agent receives only the epic title + description — no
    filesystem access.  Returns a structured ``EpicBreakdownResult``
    with parallel ``child_titles`` and ``child_bodies`` lists, and
    an optional ``epic_body`` field with a revised epic description.

    The agent is constructed via :func:`~.base.build_agent` with
    ``PromptedOutput(EpicBreakdownResult)``, ``web=False``,
    ``report_issue=False``, and ``model_name=settings.audit_model``.

    Execution is wrapped in :func:`~.retry.call_with_retry` for
    transient/rate-limit resilience.
    """
    from pydantic_ai import PromptedOutput

    from .base import build_agent, _safe_close
    from .retry import call_with_retry

    agent = build_agent(
        settings,
        system_prompt=SYSTEM_PROMPT,
        output_type=PromptedOutput(EpicBreakdownResult),
        tools=[],
        web=False,
        report_issue=False,
        model_name=settings.audit_model,
        name="epic-breakdown",
    )
    prompt = (
        f"<epic_title>{epic_title}</epic_title>\n\n"
        f"<epic_description>\n{epic_description}\n</epic_description>\n\n"
        "Break this epic into well-scoped child tickets."
    )
    try:
        result = call_with_retry(
            lambda: agent.run_sync(prompt),
            settings=settings,
            what="epic-breakdown",
        )
    finally:
        _safe_close(agent)
    return result.output
