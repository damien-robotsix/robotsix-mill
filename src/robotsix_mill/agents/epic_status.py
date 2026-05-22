"""Epic status agent: re-evaluates whether an epic's goal has been
achieved given the current state of all its child tickets.

Seam: tests monkeypatch ``run_epic_status_agent``.  The agent does NOT
get filesystem access — it only sees the structured data passed in by
the caller.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..config import Settings

SYSTEM_PROMPT = """\
You are an epic-status evaluation agent for an autonomous software
project. Your job is to examine an epic's goal (title + description)
and the current state of ALL its child tickets, and decide what should
happen to the epic.

You are called whenever a child ticket reaches DONE (merged).  Given
what the merged child actually delivered, you must **actively
reconsider the strategy**: is the remaining plan (epic description +
open children) still the right approach?  Do not just check whether
the goal is met — re-evaluate whether the strategy itself needs
revision.

Each child may have a `depends_on` field: a list of prerequisite
ticket IDs that must reach `CLOSED` or `DONE` before that child can
leave `READY` and be implemented.  This enforces a dependency chain.

Decision rules:
- **`close`** — ALL children are in terminal states (`DONE`, `CLOSED`,
  `ANSWERED`) AND the epic's stated goal appears fully satisfied. The
  epic should be closed.
- **`keep_open`** — at least one child is still in a non-terminal
  state (`DRAFT`, `READY`, `CODE_REVIEW`, `DELIVERABLE`,
  `HUMAN_MR_APPROVAL`, `REBASING`, `FIXING_CI`, `ASKED`, `BLOCKED`,
  `ERRORED`) AND the epic goal is not yet met. No change to the epic.
- **`update_description`** — the epic description must always reflect
  the current strategic plan.  If it is vague, generic, or no longer
  captures what is done and what remains, rewrite it.  A vague
  one-liner that does not lay out the global strategy IS a reason to
  rewrite.  Write a revised description that is strategic and
  specific.
- **`update_deps`** — the current dependency chain is blocking
  progress (e.g. a mid-chain child is stuck in `BLOCKED`/`ERRORED`,
  but a downstream child could proceed independently).  Provide a
  `dep_updates` dict and a human-readable `note` explaining the
  rationale.  Only use this when you are *confident* the change
  improves forward progress without breaking real prerequisites.

Be decisive.  If the strategy needs revision, revise it.  If the
description is vague or generic, rewrite it.

In addition to your main `decision`, you may also propose changes to
the child-ticket structure.  These are proposals; children that are
not in DRAFT state will be safely skipped by the worker.  You can
propose these regardless of your main `decision` (e.g. you can
`keep_open` and also propose new children):

- **`new_children`** — list of new child tickets to create.  Each must
  have a non-empty `title` and `body`.  Use this when the merged child
  reveals work that was not anticipated in the original breakdown.
- **`child_rescopes`** — map of child_id to `{"title": … (optional),
  "body": … (optional)}`.  At least one of `title` or `body` must be
  non-empty per entry.  Use this when an existing child's title or
  description is now wrong or obsolete given what just merged.
- **`child_closures`** — list of child_ids to close (transition to
  CLOSED).  Use this when a child is made obsolete by what just merged
  and should be retired.

Your `note` field must be:
- If `close`: a brief justification of why the goal is achieved (this
  becomes the transition note).
- If `keep_open`: a brief note explaining what's still outstanding.
- If `update_description`: the FULL revised epic description (this
  becomes the new `description.md` content).
- If `update_deps`: a human-readable explanation of which deps were
  changed and why (this becomes the new `description.md` content).
"""


class EpicStatusResult(BaseModel):
    decision: Literal["close", "keep_open", "update_description", "update_deps"]
    note: str = ""
    dep_updates: dict[str, list[str] | None] | None = Field(default=None)
    new_children: list[dict[str, str]] | None = Field(default=None)
    child_rescopes: dict[str, dict[str, str]] | None = Field(default=None)
    child_closures: list[str] | None = Field(default=None)


def run_epic_status_agent(
    *,
    settings: Settings,
    epic_title: str,
    epic_description: str,
    children: list[dict],
) -> EpicStatusResult:
    """Evaluate whether an epic's goal has been achieved.

    The agent receives the epic title + description and a list of
    child ticket summaries (each with ``id``, ``title``, ``state``,
    and ``description``).  Returns a structured
    ``EpicStatusResult`` with a ``decision`` and a human-readable
    ``note``.

    The agent is constructed via :func:`~.base.build_agent` with
    ``PromptedOutput(EpicStatusResult)``, ``web=False``,
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
        output_type=PromptedOutput(EpicStatusResult),
        tools=[],
        web=False,
        report_issue=False,
        model_name=settings.audit_model,
        name="epic-status",
    )
    import json

    children_json = json.dumps(children, indent=2, default=str)
    prompt = (
        f"<epic_title>{epic_title}</epic_title>\n\n"
        f"<epic_description>\n{epic_description}\n</epic_description>\n\n"
        f"<children>\n{children_json}\n</children>\n\n"
        "Evaluate the epic's status and return your decision."
    )
    try:
        result = call_with_retry(
            lambda: agent.run_sync(prompt),
            settings=settings,
            what="epic-status",
        )
    finally:
        _safe_close(agent)
    return result.output
