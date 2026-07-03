"""Coherent-set resolver for cross-repo git-rev consistency.

Given an :class:`InternalDepGraph` and the current ``main`` HEAD SHAs
for each repo, computes per-repo target ``rev`` SHAs that keep every
shared transitive dependency pinned to ONE agreed commit across the
whole transitive closure.

Also provides a ``uv lock``-based coherence check that empirically
discovers hard conflicts (the ``Requirements contain conflicting
URLs`` failure mode).
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from robotsix_mill.deps.internal_graph import InternalDepGraph

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------


@dataclass
class CoherentResolution:
    """Result of coherent-set resolution.

    *per_repo_pins* maps each *repo_id* to ``{dep_name: target_rev}``
    for every internal dep that repo pins.  Shared deps are guaranteed
    to have the same *target_rev* in every repo that pins them.

    *shared_deps* is the set of dep names pinned by ≥2 repos.
    """

    per_repo_pins: dict[str, dict[str, str]] = field(default_factory=dict)
    shared_deps: frozenset[str] = field(default_factory=frozenset)


# ---------------------------------------------------------------------------
# resolver
# ---------------------------------------------------------------------------


def resolve_coherent_set(
    dep_graph: InternalDepGraph,
    main_head_shas: dict[str, str],
) -> CoherentResolution:
    """Compute a coherent set of target pin revisions.

    For each **shared** transitive dep (pinned by ≥2 repos in
    *dep_graph*), all pinning repos must agree on a single commit.
    The resolver uses the dep's own *main_head_shas* entry as the
    agreed commit.  Non-shared deps keep their current pin revision.

    Returns a :class:`CoherentResolution` whose
    ``per_repo_pins[repo_id][dep_name]`` gives the target SHA.
    """
    # --- 1. identify shared deps -------------------------------------------
    dep_pinners: dict[str, set[str]] = {}
    for repo_id, pins in dep_graph.pins.items():
        for dep_name in pins:
            dep_pinners.setdefault(dep_name, set()).add(repo_id)

    shared_deps = frozenset(
        dep for dep, pinners in dep_pinners.items() if len(pinners) >= 2
    )

    # --- 2. build per-repo target map --------------------------------------
    per_repo_pins: dict[str, dict[str, str]] = {}
    for repo_id, pins in dep_graph.pins.items():
        targets: dict[str, str] = {}
        for dep_name, pin in pins.items():
            if dep_name in shared_deps:
                agreed = main_head_shas.get(dep_name)
                if agreed is None:
                    # main HEAD unknown → keep current pin as-is
                    targets[dep_name] = pin.rev
                else:
                    targets[dep_name] = agreed
            else:
                targets[dep_name] = pin.rev
        per_repo_pins[repo_id] = targets

    return CoherentResolution(
        per_repo_pins=per_repo_pins,
        shared_deps=shared_deps,
    )


# ---------------------------------------------------------------------------
# uv lock coherence check
# ---------------------------------------------------------------------------

_CONFLICTING_URLS_RE = re.compile(
    r"Requirements contain conflicting URLs for package (.+?):",
)


def run_coherence_check(repo_dir: Path) -> list[str]:
    """Run ``uv lock`` in *repo_dir* and parse output for conflicts.

    Returns a list of conflict descriptions (one per conflicting
    package).  An empty list means the lockfile was generated without
    ``Requirements contain conflicting URLs`` errors.

    This function **does not** raise on non-zero exit — it parses
    stderr for the known conflict pattern and returns whatever it
    finds.  The caller decides whether an empty conflict list means
    success.
    """
    result = subprocess.run(
        ["uv", "lock"],  # noqa: S607
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    combined = result.stdout + result.stderr

    conflicts: list[str] = []
    for match in _CONFLICTING_URLS_RE.finditer(combined):
        conflicts.append(match.group(0).rstrip(":"))

    if conflicts:
        logger.warning(
            "uv lock in %s reports %d conflicting package(s): %s",
            repo_dir,
            len(conflicts),
            conflicts,
        )
    return conflicts
