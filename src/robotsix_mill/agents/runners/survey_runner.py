"""Evidence-board routing for survey drafts.

A survey pass runs per repo, but a finding's evidence can belong to a
DIFFERENT registered repo (live: a pass for ``robotsix-auto-mail`` filed
"Add job-level timeout-minutes to robotsix-mill CI workflow jobs", with
mill evidence paths, on the auto-mail board — which cannot edit mill
workflows). This module rewrites the agent's result so each companion
draft lands on the board of the repo its evidence names, with the
surveyed repo kept as ``Observed on:`` evidence, and collapses duplicate
``(gap_id, target repo)`` drafts so one pass yields one ticket per repo.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ...config import RepoConfig
from .triage_boilerplate_runner import with_observed_on

log = logging.getLogger("robotsix_mill.survey_runner")

STANDARDS_REPO_ID = "robotsix-standards"


def _registered_repo_ids() -> list[str]:
    try:
        from ...config import get_repos_config

        return sorted(get_repos_config().repos.keys())
    except Exception:
        log.debug("survey: repo registry lookup failed", exc_info=True)
        return []


def _first_pos(text: str, repo_id: str) -> int | None:
    """Return where *text* first names *repo_id* (or its ``snake_case``
    package form, e.g. ``src/robotsix_mill/...``) as a whole token.
    """
    positions = [
        m.start()
        for form in {repo_id, repo_id.replace("-", "_")}
        if (m := re.search(rf"(?<![\w-]){re.escape(form)}(?![\w-])", text))
    ]
    return min(positions) if positions else None


def evidence_repo_id(
    title: str, body: str, scanned_repo_id: str, registered: list[str]
) -> str | None:
    """Return the registered repo a finding's evidence belongs to, or
    ``None`` when it cannot be told apart from the surveyed repo.

    The title is checked first, then the body. Naming the surveyed repo
    keeps the draft there (conservative — today's behaviour); a draft is
    re-routed only when it names another registered repo and not the
    surveyed one. The standards repo is never an evidence target.
    """
    candidates = [r for r in registered if r != STANDARDS_REPO_ID]
    for text in (title, body):
        if scanned_repo_id and _first_pos(text, scanned_repo_id) is not None:
            return None
        hits = [
            (pos, r)
            for r in candidates
            if r != scanned_repo_id and (pos := _first_pos(text, r)) is not None
        ]
        if hits:
            # Earliest mention wins when several repos are named.
            return min(hits)[1]
    return None


def route_survey_drafts(res: Any, repo_config: RepoConfig) -> Any:
    """Route every survey draft in *res* to its evidence board, in place.

    - A draft explicitly targeted at another repo (e.g. the standards
      board) keeps its target.
    - A repo-local draft (empty target, or the surveyed repo) goes to the
      registered repo its evidence names, falling back to the surveyed
      repo.
    - Every draft carries ``Observed on: <surveyed repo>`` in its body.
    - Drafts sharing a ``gap_id`` and target repo are collapsed to the
      first one.
    """
    scanned = repo_config.repo_id
    registered = _registered_repo_ids()
    n = min(len(res.draft_titles), len(res.draft_bodies))
    requested = list(getattr(res, "draft_target_repo_ids", None) or [])
    gap_ids = list(getattr(res, "gap_ids", None) or [])

    titles: list[str] = []
    bodies: list[str] = []
    out_gaps: list[str] = []
    targets: list[str] = []
    seen: set[tuple[str, str]] = set()
    for i in range(n):
        title, body = res.draft_titles[i], res.draft_bodies[i]
        req = requested[i] if i < len(requested) else ""
        gap = gap_ids[i] if i < len(gap_ids) else ""
        if req and req != scanned:
            target = req
        else:
            target = evidence_repo_id(title, body, scanned, registered) or scanned
            if target != scanned:
                log.info(
                    "survey: routing draft %r to %s (evidence repo) instead of %s",
                    title,
                    target,
                    scanned,
                )
        if gap and (gap, target) in seen:
            log.info(
                "survey: dropping duplicate draft %r (gap_id=%s, target=%s)",
                title,
                gap,
                target,
            )
            continue
        if gap:
            seen.add((gap, target))
        titles.append(title)
        bodies.append(with_observed_on(body, scanned))
        out_gaps.append(gap)
        # Empty target keeps the draft on the pass's own board.
        targets.append("" if target == scanned else target)

    res.draft_titles = titles
    res.draft_bodies = bodies
    res.gap_ids = out_gaps
    res.draft_target_repo_ids = targets
    return res
