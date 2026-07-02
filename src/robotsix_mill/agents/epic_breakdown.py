"""Epic breakdown agent: reads an epic description and produces a
list of well-scoped child tickets.

Seam: tests monkeypatch ``run_epic_breakdown_agent``.  The agent does
NOT get filesystem access — it only sees the epic title + description.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

import yaml as _yaml

from robotsix_mill._resources import agent_definitions_dir
from ..config import Settings
from .prerequisite import (  # cross-repo prerequisite parsing
    _IMPORT_RE,
    _PREREQ_FENCE_RE,
    _SECTION_RE,
    _SYMBOL_RE,
    _top_level_module,
)
from .prompt_blocks import section

log = logging.getLogger("robotsix_mill.agents.epic_breakdown")

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)

_SYSPROMPT_PATH = agent_definitions_dir() / "epic_breakdown.yaml"
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


# -- cross-repo prerequisite detection -----------------------------------


def _parse_prereq_packages(body: str) -> set[str]:
    """Return the set of top-level packages referenced by prerequisite
    directives in *body*.

    Only ``symbol … from <module>`` and ``import <module>`` lines
    inside a ````prereq```` fence under a ``## Prerequisites`` heading
    are considered.  Returns an empty set when no such section exists.
    """
    section_match = _SECTION_RE.search(body)
    if not section_match:
        return set()
    fence_match = _PREREQ_FENCE_RE.search(section_match.group(1))
    if not fence_match:
        return set()

    packages: set[str] = set()
    for line in fence_match.group(1).splitlines():
        line = line.strip()
        if not line:
            continue
        m = _IMPORT_RE.match(line)
        if m:
            packages.add(_top_level_module(m.group(1)))
            continue
        m = _SYMBOL_RE.match(line)
        if m:
            packages.add(_top_level_module(m.group(2)))
            continue
    return packages


#: Callable that creates a child ticket and returns its id.
#: Signature: ``(title: str, body: str) -> str``.
CreateChildFn = Callable[[str, str], str]

#: Callable that returns the board_id for a child ticket.
#: Signature: ``(child_id: str) -> str``.
BoardIdFn = Callable[[str], str]


def _build_child_repo_map(
    children: list[tuple[str, str, str]],
    child_board_id: BoardIdFn,
    repos: Any,
) -> dict[str, str]:
    """Map child_id → repo_id by resolving each child's board_id."""
    child_repo: dict[str, str] = {}
    for cid, _title, _body in children:
        board_id = child_board_id(cid)
        repo_id: str | None = None
        for rid, rc in repos.repos.items():
            if rc.board_id == board_id:
                repo_id = rid
                break
        if repo_id is not None:
            child_repo[cid] = repo_id
    return child_repo


def _build_package_repo_map(repo_ids: set[str]) -> dict[str, str]:
    """Build a package-name → repo_id map using the hyphen→underscore
    convention (e.g. 'robotsix-llmio' → 'robotsix_llmio')."""
    package_to_repo: dict[str, str] = {}
    for rid in repo_ids:
        pkg = rid.lower().replace("-", "_")
        package_to_repo[pkg] = rid
    return package_to_repo


def _create_bump_child(
    consumer_repo: str,
    producer_repo: str,
    consumer_ids: list[str],
    producer_ids: list[str],
    packages: set[str],
    create_child: CreateChildFn,
) -> tuple[str | None, dict[str, list[str]]]:
    """Create a single bump-child ticket and return its id + edges.

    Returns ``(bump_id, edges)`` where *bump_id* may be ``None`` on
    failure and *edges* maps the bump child and consumer ids to their
    dependency lists.
    """
    pkg_list = ", ".join(sorted(packages))
    title = f"Bump {consumer_repo}'s {producer_repo} pin to include {pkg_list}"
    body = (
        f"## Scope\n\n"
        f"Bump the git-rev pin of `{producer_repo}` in "
        f"`{consumer_repo}`'s `pyproject.toml` to the latest commit "
        f"on `{producer_repo}`'s default branch, then regenerate "
        f"the lockfile so that the following symbols become "
        f"importable: {pkg_list}.\n\n"
        f"## Steps\n\n"
        f"1. Determine the latest commit hash of `{producer_repo}` "
        f"on its default branch (the producer children have merged "
        f"by this point).\n"
        f"2. Update `pyproject.toml`: change the `rev` for the "
        f"`{producer_repo}` dependency to the new hash.\n"
        f"3. Run `uv lock --upgrade-package {pkg_list.split()[0]}`.\n"
        f"4. Commit both `pyproject.toml` and `uv.lock`.\n\n"
        f"## Acceptance criteria\n\n"
        f"- `pyproject.toml` pins `{producer_repo}` to a commit "
        f"that includes the symbols: {pkg_list}.\n"
        f"- `uv.lock` is regenerated and consistent.\n"
    )

    try:
        bump_id = create_child(title, body)
    except Exception:
        log.exception(
            "Failed to create bump child for %s → %s",
            consumer_repo,
            producer_repo,
        )
        return None, {}

    log.info(
        "Created bump child %s: %s (depends on %s)",
        bump_id,
        title,
        producer_ids,
    )

    edges: dict[str, list[str]] = {}
    edges[bump_id] = list(producer_ids)
    for cid in consumer_ids:
        edges.setdefault(cid, []).append(bump_id)
    return bump_id, edges


def _group_bump_candidates(
    child_prereqs: dict[str, set[str]],
    child_repo: dict[str, str],
    package_to_repo: dict[str, str],
    repo_children: dict[str, list[str]],
) -> dict[tuple[str, str], tuple[list[str], set[str]]]:
    """Group cross-repo prerequisites by (consumer_repo, producer_repo).

    Returns a dict mapping (consumer_repo, producer_repo) → (consumer_ids, packages).
    """
    candidates: dict[tuple[str, str], tuple[list[str], set[str]]] = {}
    for cid, pkgs in child_prereqs.items():
        consumer_repo = child_repo.get(cid)
        if consumer_repo is None:
            continue
        for pkg in pkgs:
            producer_repo = package_to_repo.get(pkg)
            if producer_repo is None or producer_repo == consumer_repo:
                continue
            if not repo_children.get(producer_repo):
                continue
            key = (consumer_repo, producer_repo)
            consumers, packages = candidates.setdefault(key, ([], set()))
            if cid not in consumers:
                consumers.append(cid)
            packages.add(pkg)
    return candidates


def _detect_cross_repo_deps(
    children: list[tuple[str, str, str]],
    *,
    child_board_id: BoardIdFn,
    create_child: CreateChildFn,
    repos: Any,  # ReposRegistry (import deferred to avoid cycle)
) -> tuple[dict[str, list[str]], list[str]]:
    """Detect cross-repo producer→consumer dependencies among *children*
    and create bump-child tickets that pin the producer dependency.

    Returns ``(extra_edges, bump_child_ids)``.  *extra_edges* maps
    consumer / bump child ids to dependency ids.  *bump_child_ids* is
    the list of newly-created bump child ids (callers may log them).

    When no cross-repo dependency is detected (all children share the
    same repo, or no child carries a prerequisite referencing a
    different repo among the siblings), both collections are empty.
    """
    # 1. Resolve repo per child.
    child_repo = _build_child_repo_map(children, child_board_id, repos)
    repo_ids_present = set(child_repo.values())
    if len(repo_ids_present) <= 1:
        return {}, []

    # 2. Build repo → package-name mapping (convention-based).
    package_to_repo = _build_package_repo_map(repo_ids_present)

    # 3. Collect prerequisite packages per child.
    child_prereqs: dict[str, set[str]] = {
        cid: pkgs
        for cid, _t, body in children
        if (pkgs := _parse_prereq_packages(body))
    }

    # 4. Children per repo (potential producers).
    repo_children: dict[str, list[str]] = {}
    for cid, repo in child_repo.items():
        repo_children.setdefault(repo, []).append(cid)

    # 5. Group cross-repo deps by (consumer_repo, producer_repo) pair.
    candidates = _group_bump_candidates(
        child_prereqs,
        child_repo,
        package_to_repo,
        repo_children,
    )
    if not candidates:
        return {}, []

    # 6. Create bump children and wire edges.
    extra_edges: dict[str, list[str]] = {}
    bump_ids: list[str] = []
    for (consumer_repo, producer_repo), (consumer_ids, packages) in candidates.items():
        producer_ids = repo_children.get(producer_repo, [])
        if not producer_ids:
            continue
        bump_id, edges = _create_bump_child(
            consumer_repo,
            producer_repo,
            consumer_ids,
            producer_ids,
            packages,
            create_child,
        )
        if bump_id is not None:
            bump_ids.append(bump_id)
            extra_edges.update(edges)

    return extra_edges, bump_ids


def _run_cross_repo_detection(
    children: list[tuple[str, str, str]],
    child_board_id: BoardIdFn,
    create_child: CreateChildFn,
) -> dict[str, list[str]]:
    """Run cross-repo detection and return bump/consumer edges.

    Returns an empty dict when repos config is unavailable or no
    cross-repo dependency is detected.
    """
    try:
        from ..config.repos import get_repos_config  # noqa: E402

        repos = get_repos_config()
        if repos is None:
            return {}
        cross_repo_edges, _bump_ids = _detect_cross_repo_deps(
            children,
            child_board_id=child_board_id,
            create_child=create_child,
            repos=repos,
        )
        return cross_repo_edges
    except Exception:
        log.debug(
            "Cross-repo dependency detection skipped (repos config unavailable?)",
            exc_info=True,
        )
        return {}


def _compute_base_edges(
    children: list[tuple[str, str, str]],
    predecessor_id: str | None,
) -> dict[str, list[str]]:
    """Compute init-repo or linear-chain edges for *children*.

    Returns a ``{child_id: [dep_id, …]}`` map.
    """
    edges: dict[str, list[str]] = {}
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
    else:
        for i, (cid, _t, _b) in enumerate(children):
            if i == 0:
                if predecessor_id is not None:
                    edges[cid] = [predecessor_id]
            else:
                edges[cid] = [children[i - 1][0]]
    return edges


def plan_child_dependencies(
    children: list[tuple[str, str, str]],
    *,
    predecessor_id: str | None = None,
    child_board_id: BoardIdFn | None = None,
    create_child: CreateChildFn | None = None,
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

    When *child_board_id* and *create_child* are both provided, the
    function also detects **cross-repo producer→consumer dependencies**
    among the children: if a consumer child (target repo B) imports
    symbols from a producer sibling repo (target repo A, A != B) that
    the consumer git-rev-pins, a synthetic bump-child ticket is created
    that bumps B's pin of A to include those symbols.  The consumer
    depends on the bump child, and the bump child depends on the
    producer children, so the consumer stays blocked (``unmet_deps``)
    until the pin is bumped.

    Returns a ``{child_id: [dep_id, …]}`` map covering only the children
    that gain an edge; callers apply it via ``set_depends_on``.
    """
    if not children:
        return {}

    # 1. Base edges (init-repo or linear chain).
    edges = _compute_base_edges(children, predecessor_id)

    # 2. Cross-repo edges (additive).
    if child_board_id is not None and create_child is not None:
        for cid, deps in _run_cross_repo_detection(
            children, child_board_id, create_child
        ).items():
            if cid in edges:
                edges[cid].extend(deps)
            else:
                edges[cid] = deps

    return edges


class EpicBreakdownResult(BaseModel):
    """Structured result of breaking an epic into child tickets.

    Holds parallel ``child_titles``, ``child_bodies``, and
    ``child_repo_ids`` lists (one entry per proposed child ticket)
    plus an optional ``epic_body`` carrying a revised epic description
    when the agent reworked it.

    ``PromptedOutput`` describes the schema in the prompt but does not
    enforce it, and models (haiku AND opus alike, observed live) reliably
    emit the children under the natural keys ``titles`` / ``bodies``
    rather than ``child_titles`` / ``child_bodies`` — which silently
    parsed to empty lists, so the epic spawned zero children. The
    ``validation_alias`` ``AliasChoices`` accept BOTH the canonical and
    the natural keys so the parse succeeds regardless of which the model
    emits; ``populate_by_name`` keeps construction by the field name
    working (tests, internal callers).
    """

    model_config = ConfigDict(populate_by_name=True)

    child_titles: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("child_titles", "titles"),
    )
    child_bodies: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("child_bodies", "bodies"),
    )
    child_repo_ids: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("child_repo_ids", "repo_ids"),
    )
    epic_body: str | None = None


def run_epic_breakdown_agent(
    *,
    settings: Settings,
    epic_title: str,
    epic_description: str,
    comments: str = "",
    available_repos: list[tuple[str, str]] | None = None,
    epic_repo_id: str = "",
) -> EpicBreakdownResult:
    """Break an epic into well-scoped child tickets.

    The agent receives only the epic title + description — no
    filesystem access.  Returns a structured ``EpicBreakdownResult``
    with parallel ``child_titles``, ``child_bodies``, and
    ``child_repo_ids`` lists, and an optional ``epic_body`` field
    with a revised epic description.

    When *comments* is non-empty, the operator's comment history is
    appended to the prompt in an ``<operator_comments>`` block so the
    agent can follow the operator's explicit direction.

    When *available_repos* is provided (a list of ``(repo_id, board_id)``
    tuples), a section listing valid repo IDs is injected into the prompt
    so the agent can assign each child to the correct repo. *epic_repo_id*
    names the epic's own repo (the safe default for any child whose repo
    is unspecified).

    The agent is constructed via :func:`~.base.build_agent` with
    ``PromptedOutput(EpicBreakdownResult)``, ``web=False``,
    ``report_issue=False``, and the epic_breakdown definition's
    ``level: 3`` (Claude Opus). Decomposition emits a structured
    ``PromptedOutput`` whose field names a cheap model (e.g. the
    ``audit_model`` tier — haiku under the Claude SDK backend) fails to
    honor: it returns the children under ``titles``/``bodies`` rather
    than ``child_titles``/``child_bodies``, so the parser silently
    yields zero children. The default model is reliable here.

    Execution is wrapped in :func:`~.retry.call_with_retry` for
    transient/rate-limit resilience.
    """
    from .yaml_loader import load_and_run_agent

    prompt = (
        section("epic-title", epic_title)
        + "\n\n"
        + section("epic-description", epic_description)
    )
    if available_repos:
        repo_lines = [f"- ``{repo_id}``" for repo_id, _board_id in available_repos]
        repo_list = "\n".join(repo_lines)
        prompt += "\n\n" + section(
            "available-repos",
            f"The epic lives in ``{epic_repo_id or 'default'}``. "
            f"Valid target repos:\n{repo_list}\n\n"
            "Assign each child to the repo where its code/work "
            "actually lives. When unsure, use the epic's own repo "
            "(or omit the entry — it defaults safely).",
        )
    if comments:
        prompt += "\n\n" + section("operator-comments", comments)
    prompt += "\n\nBreak this epic into well-scoped child tickets."
    result = load_and_run_agent(
        settings=settings,
        definition_name="epic_breakdown",
        tools=[],
        prompt=prompt,
        what="epic-breakdown",
    )
    return result.output
