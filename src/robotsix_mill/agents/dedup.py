"""Pre-refine dedup / already-done check.

A single cheap LLM call that inspects a draft against existing tickets
to decide whether it's a duplicate or already implemented.  The
refiner would be wasted on such drafts — this guard short-circuits
them straight to ``CLOSED`` before the expensive agent runs.

``run_dedup_check`` is the mockable seam — tests monkeypatch it just
like ``run_refine_agent``.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from pydantic import BaseModel

from ..config import Settings
from ..core.models import Ticket
from .prompt_blocks import section


class DedupResult(BaseModel):
    """Structured output from the dedup check agent."""

    duplicate_of: str | None = None
    already_done: str | None = None
    reason: str = ""


log = logging.getLogger("robotsix_mill.agents.dedup")


def tokenize(text: str) -> set[str]:
    """Tokenize *text* for Jaccard similarity: lowercase, split on
    non-alphanumeric characters, keep tokens longer than 2 chars."""
    return set(
        t for t in re.sub(r"[^a-z0-9]+", " ", text.casefold()).split() if len(t) > 2
    )


def any_candidate_overlap(
    *,
    draft_title: str,
    draft_body: str,
    candidates_texts: list[str],
) -> bool:
    """True iff the draft shares at least one meaningful token with any
    candidate's title or body. Used to skip the LLM dedup call when no
    candidate could plausibly be a duplicate.

    *candidates_texts* are the caller-assembled title+body strings for
    each candidate (kept as plain strings so this helper stays pure and
    unit-testable). Returns ``False`` for an empty candidate list or an
    empty draft token set; returns ``True`` on the first non-empty
    token intersection.
    """
    draft_tokens = tokenize(draft_title + " " + draft_body)
    if not draft_tokens:
        return False
    for text in candidates_texts:
        if draft_tokens & tokenize(text):
            return True
    return False


def rank_candidates_by_similarity(
    *,
    draft_title: str,
    draft_body: str,
    candidates: list[Ticket],
    max_candidates: int,
) -> list[Ticket]:
    """Return the top-*max_candidates* tickets ranked by Jaccard
    similarity to the draft.

    When ``len(candidates) <= max_candidates``, returns the list
    unchanged (no-op for small repos).  Otherwise tokenizes the draft
    and each candidate's title, computes the Jaccard coefficient, sorts
    descending, and returns the top *max_candidates*.
    """
    if len(candidates) <= max_candidates:
        return list(candidates)

    draft_tokens = tokenize(draft_title + " " + draft_body)
    if not draft_tokens:
        # No meaningful tokens in the draft — graceful degradation:
        # return the first max_candidates as-is.
        return list(candidates[:max_candidates])

    scored: list[tuple[float, Ticket]] = []
    for cand in candidates:
        cand_tokens = tokenize(cand.title)
        if not cand_tokens:
            scored.append((0.0, cand))
            continue
        intersection = draft_tokens & cand_tokens
        union = draft_tokens | cand_tokens
        jaccard = len(intersection) / len(union)
        scored.append((jaccard, cand))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    scored = scored[:max_candidates]
    return [cand for _, cand in scored]


def _build_prompt(
    *,
    draft_title: str,
    draft_body: str,
    candidates_json: str,
) -> str:
    draft_block = "\n".join(
        [
            section("title", draft_title),
            section("body", draft_body),
        ]
    )
    return (
        section("draft", draft_block) + "\n\n" + section("candidates", candidates_json)
    )


def run_dedup_check(
    *,
    settings: Settings,
    draft_title: str,
    draft_body: str,
    candidates_json: str,
    repo_dir: Path | None = None,
) -> dict:
    """Return ``{"duplicate_of": ..., "already_done": ..., "reason": ...}``.

    Degrades gracefully: on any exception, returns nulls with a failure
    reason — the guard is best-effort and never blocks the pipeline.
    """
    from .base import build_agent_from_definition, _safe_close
    from pydantic_ai.usage import UsageLimits

    from .yaml_loader import load_agent_definition
    from .retry import run_agent
    from ..data_paths import data_dir

    definition = load_agent_definition(data_dir("agent_definitions") / "dedup.yaml")

    # Build filesystem tools when a repo_dir is available; the dedup
    # agent is read-only — only read_file and list_dir are exposed.
    tools: list = []
    if repo_dir is not None:
        from .fs_tools import build_fs_tools

        fs = build_fs_tools(repo_dir, settings)
        tools = [t for t in fs if t.__name__ in ("read_file", "list_dir")]

    log.info("dedup check using level: %s", definition.level)
    agent = build_agent_from_definition(
        settings,
        definition,
        tools=tools,
    )
    # request_limit must be passed via usage_limits=UsageLimits(...),
    # NOT as a bare run_sync kwarg — the bare kwarg raises
    # UserError("Unknown keyword arguments: request_limit"), which made
    # the dedup check ALWAYS fail (caught best-effort), so dd73 never
    # actually deduped — hence the overlapping-PR churn it was meant to
    # stop. Mirrors coordinating/explore/testing.
    limits = UsageLimits(request_limit=settings.dedup_request_limit)
    try:
        result = run_agent(
            agent,
            lambda h: h.run_sync(
                _build_prompt(
                    draft_title=draft_title,
                    draft_body=draft_body,
                    candidates_json=candidates_json,
                ),
                usage_limits=limits,
            ),
            what="dedup check",
        )
        output = result.output
        if not isinstance(output, DedupResult):
            log.warning("dedup check returned non-DedupResult: %s", type(output))
            return {
                "duplicate_of": None,
                "already_done": None,
                "reason": "dedup check returned unexpected type",
            }
        return {
            "duplicate_of": output.duplicate_of,
            "already_done": output.already_done,
            "reason": output.reason,
        }
    except Exception:
        log.warning("dedup check failed, proceeding with refine", exc_info=True)
        return {
            "duplicate_of": None,
            "already_done": None,
            "reason": "dedup check failed",
        }
    finally:
        _safe_close(agent)
