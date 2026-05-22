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

You are called whenever a child ticket reaches DONE (merged).  Your
task is to determine whether the epic goal is now satisfied, still in
progress, or needs its description updated to reflect remaining work.

Decision rules:
- **`close`** — ALL children are in terminal states (`DONE`, `CLOSED`,
  `ANSWERED`) AND the epic's stated goal appears fully satisfied. The
  epic should be closed.
- **`keep_open`** — at least one child is still in a non-terminal
  state (`DRAFT`, `READY`, `CODE_REVIEW`, `DELIVERABLE`,
  `HUMAN_MR_APPROVAL`, `REBASING`, `FIXING_CI`, `ASKED`, `BLOCKED`,
  `ERRORED`) AND the epic goal is not yet met. No change to the epic.
- **`update_description`** — some children are done, but the epic
  description no longer accurately reflects the remaining scope. Write
  a revised description that captures what's done and what remains.
  Use this sparingly — only when the description is genuinely stale.

Be decisive: only choose `close` when you are confident the goal is
achieved.  When in doubt, prefer `keep_open`.

Your `note` field must be:
- If `close`: a brief justification of why the goal is achieved (this
  becomes the transition note).
- If `keep_open`: a brief note explaining what's still outstanding.
- If `update_description`: the FULL revised epic description (this
  becomes the new `description.md` content).
"""


class EpicStatusResult(BaseModel):
    decision: Literal["close", "keep_open", "update_description"]
    note: str = ""


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
