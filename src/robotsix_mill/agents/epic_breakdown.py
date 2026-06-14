"""Epic breakdown agent: reads an epic description and produces a
list of well-scoped child tickets.

Seam: tests monkeypatch ``run_epic_breakdown_agent``.  The agent does
NOT get filesystem access — it only sees the epic title + description.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from ..config import Settings
from .prompt_blocks import section

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)
import yaml as _yaml

_SYSPROMPT_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "agent_definitions"
    / "epic_breakdown.yaml"
)
SYSTEM_PROMPT: str = _yaml.safe_load(_SYSPROMPT_PATH.read_text())["system_prompt"]

# A decomposition child is an *init-repo* action when it creates /
# initializes a brand-new repository (the maintenance ``create_repo``
# step that registers the repo in ``config/repos.yaml``).  Matches
# "create repo[sitory]" and "initialize/initialise/scaffold/bootstrap/
# set up … repository" — e.g. "Initialize communication system
# repository".
_INIT_REPO_RE = re.compile(
    r"create\s+repo(?:sitor(?:y|ies))?\b"
    r"|\b(?:initiali[sz]e|scaffold|bootstrap|set\s*up)\b[^\n]*?"
    r"\brepositor(?:y|ies)\b",
    re.IGNORECASE,
)


def _is_init_repo_child(title: str, body: str) -> bool:
    """True when an epic-decomposition child is a create/initialize-repo action."""
    return bool(_INIT_REPO_RE.search(title) or _INIT_REPO_RE.search(body))


def plan_child_dependencies(
    children: list[tuple[str, str, str]],
    *,
    predecessor_id: str | None = None,
) -> dict[str, list[str]]:
    """Compute ``depends_on`` edges for freshly-created epic children.

    *children* is an ordered list of ``(child_id, title, body)`` tuples,
    one per newly-created child in agent order.  *predecessor_id*, when
    given, is an existing sibling the new batch is appended after (the
    worker re-process path chains the batch onto the last existing
    child).

    Default behaviour is the existing linear chain ``C0 → C1 → C2 …``
    (each child depends on the previous), anchored to *predecessor_id*
    when supplied.

    When the batch contains an **init-repo** child (a create/initialize-
    repository action that registers a brand-new repo in
    ``config/repos.yaml``), the wiring changes: every OTHER child depends
    on that init-repo child instead, so the repo-populating siblings stay
    blocked (``unmet_deps``) until the init-repo child closes and the
    repo is registered.  This stops a populating child from running — and
    misrouting its output to the wrong repo — before its target repo
    exists.

    Returns a ``{child_id: [dep_id, …]}`` map covering only the children
    that gain an edge; callers apply it via ``set_depends_on``.
    """
    edges: dict[str, list[str]] = {}
    if not children:
        return edges

    init_idx = next(
        (i for i, (_cid, t, b) in enumerate(children) if _is_init_repo_child(t, b)),
        None,
    )
    if init_idx is not None:
        init_id = children[init_idx][0]
        if predecessor_id is not None:
            edges[init_id] = [predecessor_id]
        for i, (cid, _t, _b) in enumerate(children):
            if i != init_idx:
                edges[cid] = [init_id]
        return edges

    # No init-repo child — preserve the existing linear chain.
    for i, (cid, _t, _b) in enumerate(children):
        if i == 0:
            if predecessor_id is not None:
                edges[cid] = [predecessor_id]
        else:
            edges[cid] = [children[i - 1][0]]
    return edges


class EpicBreakdownResult(BaseModel):
    """Structured result of breaking an epic into child tickets.

    Holds parallel ``child_titles`` and ``child_bodies`` lists (one
    entry per proposed child ticket) plus an optional ``epic_body``
    carrying a revised epic description when the agent reworked it.
    """

    child_titles: list[str] = Field(default_factory=list)
    child_bodies: list[str] = Field(default_factory=list)
    epic_body: str | None = None


def run_epic_breakdown_agent(
    *,
    settings: Settings,
    epic_title: str,
    epic_description: str,
    comments: str = "",
) -> EpicBreakdownResult:
    """Break an epic into well-scoped child tickets.

    The agent receives only the epic title + description — no
    filesystem access.  Returns a structured ``EpicBreakdownResult``
    with parallel ``child_titles`` and ``child_bodies`` lists, and
    an optional ``epic_body`` field with a revised epic description.

    When *comments* is non-empty, the operator's comment history is
    appended to the prompt in an ``<operator_comments>`` block so the
    agent can follow the operator's explicit direction.

    The agent is constructed via :func:`~.base.build_agent` with
    ``PromptedOutput(EpicBreakdownResult)``, ``web=False``,
    ``report_issue=False``, and ``model_name=settings.audit_model``.

    Execution is wrapped in :func:`~.retry.call_with_retry` for
    transient/rate-limit resilience.
    """
    from .yaml_loader import load_and_run_agent

    prompt = (
        section("epic-title", epic_title)
        + "\n\n"
        + section("epic-description", epic_description)
    )
    if comments:
        prompt += "\n\n" + section("operator-comments", comments)
    prompt += "\n\nBreak this epic into well-scoped child tickets."
    result = load_and_run_agent(
        settings=settings,
        definition_name="epic_breakdown",
        tools=[],
        model_name=settings.audit_model,
        prompt=prompt,
        what="epic-breakdown",
    )
    return result.output
