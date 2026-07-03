"""Epic status agent: re-evaluates whether an epic's goal has been
achieved given the current state of all its child tickets.

Seam: tests monkeypatch ``run_epic_status_agent``.  The agent does NOT
get filesystem access — it only sees the structured data passed in by
the caller.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, ConfigDict

from ..config import Settings
from .prompt_blocks import section


class EpicStatusResult(BaseModel):
    """Structured verdict on whether an epic's goal has been achieved.

    ``decision`` is the action to take on the epic (``close``,
    ``keep_open``, ``update_description``, or ``update_deps``) and
    ``note`` is a human-readable rationale. The remaining optional
    fields carry the concrete mutations a decision may imply:
    ``dep_updates`` (per-child dependency rewrites), ``new_children``
    (additional child tickets to file), ``child_rescopes`` (per-child
    title/body rewrites), and ``child_closures`` (a map of child ID ->
    the merged covering sibling whose delivered scope obsoletes it).

    ``child_closures`` is a ``dict[child_id -> covering_sibling_id]``.
    A legacy bare ``list[str]`` (child IDs with no named covering
    sibling) is still accepted for robustness and normalized by the
    worker — but such entries carry no covering sibling and are refused
    by the delivery-evidence gate.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    decision: Literal["close", "keep_open", "update_description", "update_deps"]
    note: str = ""
    dep_updates: dict[str, list[str] | None] | None = Field(default=None)
    new_children: list[dict[str, str]] | None = Field(default=None)
    child_rescopes: dict[str, dict[str, str]] | None = Field(default=None)
    child_closures: dict[str, str] | list[str] | None = Field(default=None)


def _build_children_table(children: list[dict]) -> str:
    """Build a compact Markdown table of child tickets.

    Columns: ID, Title, State, Delivery, Deps, Summary.  The Delivery
    column reports whether the child actually shipped scope (``merged``)
    vs delivered nothing (``dedup-closed``/``unstarted``/in-progress).
    The Summary column is the first ~300 chars of the child's
    description with newlines collapsed to spaces and pipe characters
    escaped.  Deps are shown as a comma-separated list or ``-`` if none.
    """
    if not children:
        return "(none)"

    header = "| ID | Title | State | Delivery | Deps | Summary |"
    sep = "|---|---|---|---|---|---|"
    rows: list[str] = [header, sep]

    for child in children:
        cid = _escape_pipe(child.get("id", ""))
        title = _escape_pipe(child.get("title", ""))
        state = _escape_pipe(child.get("state", ""))
        delivery = _escape_pipe(child.get("delivery", ""))

        deps_list = child.get("depends_on") or []
        deps = ", ".join(deps_list) if deps_list else "-"

        desc: str = child.get("description", "")
        # Collapse newlines to a single space and escape pipe chars.
        desc = desc.replace("\n", " ").replace("|", "\\|")
        if len(desc) > 300:
            desc = desc[:300] + "..."

        rows.append(f"| {cid} | {title} | {state} | {delivery} | {deps} | {desc} |")

    return "\n".join(rows)


def _escape_pipe(value: str) -> str:
    """Escape literal pipe characters so they don't break the table."""
    return value.replace("|", "\\|")


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
    ``report_issue=False``, and the epic_status definition's ``level: 1``.

    Execution is wrapped in :func:`~.retry.call_with_retry` for
    transient/rate-limit resilience.
    """
    from .yaml_loader import load_and_run_agent

    children_table = _build_children_table(children)
    prompt = (
        section("epic-title", epic_title)
        + "\n\n"
        + section("epic-description", epic_description)
        + "\n\n"
        + section("children", children_table)
        + "\n\n"
        + "Evaluate the epic's status and return your decision."
    )
    result = load_and_run_agent(
        settings=settings,
        definition_name="epic_status",
        tools=[],
        prompt=prompt,
        what="epic-status",
    )
    return result.output
