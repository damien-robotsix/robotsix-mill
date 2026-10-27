"""Owner-board routing for triage_boilerplate findings.

Every pattern the triage_boilerplate pass spots (``CI failure: X on
main`` noise, refine/ci_fix churn, scope-triage blocks) is behaviour of
the mill's own stages or of the shared reusable workflows — never of the
scanned repo. Filing it on the scanned repo's board made it
unimplementable there (external-scope gate / human approval, then a
manual re-file on the mill board). This module rewrites the agent's
result so each draft lands on the OWNING board, with the scanned repo
kept as evidence in the body.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ...config import RepoConfig

log = logging.getLogger("robotsix_mill.triage_boilerplate_runner")

MILL_REPO_ID = "robotsix-mill"
WORKFLOWS_REPO_ID = "robotsix-github-workflows"
OWNER_REPO_IDS: frozenset[str] = frozenset({MILL_REPO_ID, WORKFLOWS_REPO_ID})

# Reusable-workflow patterns belong to the github-workflows board; every
# other pattern (ci source, refine, ci_fix, scope-triage) to the mill.
_WORKFLOW_PATTERN_RE = re.compile(
    r"reusable[- ]workflow|robotsix-github-workflows|workflow_call",
    re.IGNORECASE,
)

OBSERVED_ON_PREFIX = "Observed on:"


def _registered_repo_ids() -> set[str]:
    try:
        from ...config import get_repos_config

        return set(get_repos_config().repos.keys())
    except Exception:
        log.debug("triage_boilerplate: repo registry lookup failed", exc_info=True)
        return set()


def owner_repo_id(title: str, body: str, requested: str = "") -> str:
    """Return the repo_id whose board owns the behaviour in a finding.

    An explicit agent choice is honoured only when it is one of the
    owner boards; anything else (empty, or the scanned repo) is
    replaced by the deterministic classification. The workflows board
    falls back to the mill when it is not registered.
    """
    if requested in OWNER_REPO_IDS:
        choice = requested
    elif _WORKFLOW_PATTERN_RE.search(f"{title}\n{body}"):
        choice = WORKFLOWS_REPO_ID
    else:
        choice = MILL_REPO_ID
    if choice == WORKFLOWS_REPO_ID and WORKFLOWS_REPO_ID not in _registered_repo_ids():
        choice = MILL_REPO_ID
    return choice


def with_observed_on(body: str, repo_id: str) -> str:
    """Append ``Observed on: <repo_id>`` evidence unless already present."""
    line = f"{OBSERVED_ON_PREFIX} {repo_id}"
    if line in body:
        return body
    return f"{body.rstrip()}\n\n{line}"


def route_triage_boilerplate_drafts(res: Any, repo_config: RepoConfig) -> Any:
    """Route every draft in *res* to its owner board, in place.

    Sets ``draft_target_repo_ids`` for each draft (so none falls back
    to the scanned repo's board) and records the scanned repo as
    evidence in the body.
    """
    n = min(len(res.draft_titles), len(res.draft_bodies))
    requested = list(getattr(res, "draft_target_repo_ids", None) or [])
    targets: list[str] = []
    for i in range(n):
        req = requested[i] if i < len(requested) else ""
        targets.append(owner_repo_id(res.draft_titles[i], res.draft_bodies[i], req))
        res.draft_bodies[i] = with_observed_on(res.draft_bodies[i], repo_config.repo_id)
    res.draft_target_repo_ids = targets
    return res
