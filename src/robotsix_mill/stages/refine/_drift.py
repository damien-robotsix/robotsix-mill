"""Deterministic spec-drift guard for the refine stage.

A refine run rewrites the ticket's description (and, when the agent
supplies one, its title). Under prompt pressure — a 60 KB user prompt
where the 4 KB draft sits between a standards dump and the repo-level
memory ledger, served by a level-1 fallback model — the agent can lose
the referent and write a spec for a *different* task it saw in the
reference material. Observed live on 2026-09-07 (mill ticket
``20260907T120541Z-auto-approve-triage-failed-fallback-rout-a144``):
the draft asked to fix the auto-approve "triage failed" fallback; the
refined spec (and new title) asked to add docstrings to
``config/repo_settings.py``, a topic lifted from the memory ledger. The
original capability vanished from the board with no history event.

This guard is purely lexical, so it is model-independent: it takes the
ticket title's anchor terms (long-ish, non-generic words) and requires
the refined output to mention a minimum share of them (prefix-stemmed,
so ``approve`` matches ``approval``). A title too vague to judge (fewer
than ``refine_drift_guard_min_anchors`` anchors) is never judged: the
draft body is deliberately NOT used as a fallback, because CI-failure
drafts (raw logs) are legitimately refined into root-cause specs with a
different vocabulary and would trip the guard. On a miss the refine
result is NOT persisted: title, description and memory stay untouched,
the rejected spec is saved as an artifact, and the ticket parks in
``HUMAN_ISSUE_APPROVAL`` with a note naming both titles so the drain
session or operator can decide (approve to implement the raw draft, or
send back to ``DRAFT`` for another refine).
"""

from __future__ import annotations

import re

from ...agents import refining
from ...config.settings import Settings
from ...core.models import Ticket
from ...core.states import State
from ...core.workspace import Workspace
from ..base import Outcome
from .helpers import log

#: Words that carry no subject information on a ticket board; they are
#: never used as anchors even when long enough.
_GENERIC_TERMS: frozenset[str] = frozenset(
    {
        "about",
        "accept",
        "acceptance",
        "added",
        "adding",
        "after",
        "agent",
        "agents",
        "allow",
        "always",
        "avoid",
        "based",
        "before",
        "behavior",
        "behaviour",
        "better",
        "board",
        "change",
        "changes",
        "check",
        "checks",
        "clean",
        "code",
        "config",
        "correct",
        "correctly",
        "create",
        "current",
        "default",
        "detect",
        "during",
        "enable",
        "ensure",
        "error",
        "errors",
        "every",
        "expected",
        "explicit",
        "failed",
        "failing",
        "fails",
        "failure",
        "failures",
        "feature",
        "field",
        "fields",
        "file",
        "files",
        "first",
        "fleet",
        "handle",
        "handling",
        "improve",
        "instead",
        "issue",
        "issues",
        "keep",
        "logic",
        "longer",
        "make",
        "makes",
        "making",
        "match",
        "method",
        "missing",
        "model",
        "module",
        "never",
        "option",
        "other",
        "output",
        "problem",
        "process",
        "proper",
        "properly",
        "reduce",
        "remove",
        "repo",
        "repos",
        "repository",
        "request",
        "requests",
        "result",
        "results",
        "return",
        "returns",
        "robotsix",
        "running",
        "same",
        "scope",
        "should",
        "shows",
        "single",
        "small",
        "spec",
        "stage",
        "stages",
        "state",
        "states",
        "still",
        "support",
        "surface",
        "system",
        "target",
        "tests",
        "their",
        "there",
        "these",
        "thing",
        "things",
        "those",
        "ticket",
        "tickets",
        "unify",
        "update",
        "updates",
        "using",
        "value",
        "values",
        "where",
        "which",
        "while",
        "whole",
        "without",
        "would",
        "wrong",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_MIN_ANCHOR_LEN = 5
#: Shared prefix length that counts as the same stem (``approve`` /
#: ``approval`` / ``approving`` all share ``appro``).
_STEM_LEN = 5


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _is_anchor(token: str) -> bool:
    return (
        len(token) >= _MIN_ANCHOR_LEN
        and token not in _GENERIC_TERMS
        and not token.isdigit()
    )


def title_anchors(title: str) -> set[str]:
    """Return the subject-bearing terms of *title* (deduplicated)."""
    return {t for t in _tokens(title) if _is_anchor(t)}


def anchor_overlap(anchors: set[str], text: str) -> float:
    """Share of *anchors* whose stem appears somewhere in *text*.

    Returns 1.0 for an empty anchor set (nothing to contradict).
    """
    if not anchors:
        return 1.0
    stems = {t[:_STEM_LEN] for t in _tokens(text) if len(t) >= _STEM_LEN}
    hits = sum(1 for a in anchors if a[:_STEM_LEN] in stems)
    return hits / len(anchors)


def refined_text(result: refining.RefineResult) -> str:
    """Concatenate everything the refine result would persist as the spec."""
    parts: list[str] = [result.title or "", result.spec_markdown or ""]
    for child in result.children or []:
        parts.append(child.title)
        parts.append(child.spec_markdown)
    if result.epic_body:
        parts.append(result.epic_body)
    return "\n".join(p for p in parts if p)


def detect_spec_drift(
    title: str,
    draft: str,
    result: refining.RefineResult,
    *,
    min_overlap: float,
    min_anchors: int,
) -> tuple[bool, str]:
    """Return ``(drifted, detail)`` for *result* against the original ticket.

    Only the title's anchors are judged; a title with fewer than
    *min_anchors* anchors is never judged (``drifted=False``). *draft* is
    accepted for signature stability and future use but does not take part
    in the decision (see the module docstring). *detail* is a short
    human-readable account of the comparison for logs and notes.
    """
    del draft  # not part of the decision — see module docstring
    text = refined_text(result)
    if not text.strip():
        return False, "empty refine result — nothing to compare"

    anchors = title_anchors(title)
    if len(anchors) < min_anchors:
        return (
            False,
            f"too few title anchor terms to judge ({len(anchors)} < {min_anchors})",
        )

    overlap = anchor_overlap(anchors, text)
    detail = (
        f"{overlap:.0%} of the original title's {len(anchors)} anchor "
        f"terms appear in the refined spec ({', '.join(sorted(anchors))})"
    )
    return overlap < min_overlap, detail


def spec_drift_guard(
    ticket: Ticket,
    title: str,
    draft: str,
    ws: Workspace,
    s: Settings,
    result: refining.RefineResult,
) -> Outcome | None:
    """Park the ticket instead of persisting a refine result that changed subject.

    Runs before any title/description/memory side-effect. Returns ``None``
    when the guard is disabled, when there is too little to judge, or
    when the refined spec still talks about the ticket's subject.
    """
    if not s.refine_drift_guard_enabled:
        return None
    if result.no_change_needed or result.promote_to_epic:
        # A NO_CHANGE rationale legitimately explains why nothing is to be
        # done, and a promote-to-epic body is a strategic summary whose
        # subject lives in the breakdown children — neither has to
        # restate the title.
        return None

    drifted, detail = detect_spec_drift(
        title,
        draft,
        result,
        min_overlap=s.refine_drift_guard_min_overlap,
        min_anchors=s.refine_drift_guard_min_anchors,
    )
    if not drifted:
        return None

    new_title = (result.title or "").strip() or "(unchanged)"
    log.warning(
        "%s: refine drift guard — refined spec does not mention the ticket's "
        "subject (%s); original title %r, proposed title %r — result discarded",
        ticket.id,
        detail,
        title,
        new_title,
    )
    try:
        ws.artifacts_dir.mkdir(parents=True, exist_ok=True)
        (ws.artifacts_dir / "refine-drift-rejected.md").write_text(
            f"# Rejected refine result (spec drift)\n\n"
            f"- original title: {title}\n"
            f"- proposed title: {new_title}\n"
            f"- {detail}\n\n"
            f"## Proposed spec\n\n{refined_text(result)}\n",
            encoding="utf-8",
        )
    except OSError:
        log.warning(
            "%s: could not write refine-drift artifact", ticket.id, exc_info=True
        )

    return Outcome(
        State.HUMAN_ISSUE_APPROVAL,
        f"refine drift guard: the refined spec changed subject — {detail}. "
        f"Original title: {title!r}; proposed title: {new_title!r}. "
        "The draft and title were left untouched (rejected spec saved as "
        "artifact refine-drift-rejected.md). Approve to implement the raw "
        "draft, or move back to draft to refine again.",
    )
