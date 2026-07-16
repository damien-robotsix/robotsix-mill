"""The meta-agent reasoning core: cross-repo analysis for shared
abstractions and practice divergence.

Child 3 of the meta-agent epic — a self-contained primitive that the
meta-pass runner (child 6) wires into the full pipeline.

The agent receives all registered repo clones, surveys each one, and
emits structured ``DraftProposal`` lists (extraction + alignment) plus
an updated memory ledger.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from robotsix_mill._resources import agent_definitions_dir
from ..config import Settings

# ---------------------------------------------------------------------------
# Load the static system prompt from the YAML definition
# ---------------------------------------------------------------------------

import yaml as _yaml

from ..agents.periodic_base import load_periodic_system_prompt

SYSTEM_PROMPT: str = load_periodic_system_prompt("meta")


MAX_PROPOSALS = 10


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class DraftProposal(BaseModel):
    """A single structured proposal emitted by the meta-agent.

    *Extraction* drafts (shared abstractions → extract into a
    standalone library) leave ``target_repo_id`` as ``None`` — these
    are destined for the meta board.

    *Alignment* drafts (practice divergence → one repo should adopt
    another's pattern) set ``target_repo_id`` to the repo that needs
    the improvement.

    *TODO* drafts (resolving an outstanding ``TODO`` / ``FIXME``
    marker found in a repo clone) set ``target_repo_id`` to the repo
    the marker lives in.
    """

    title: str
    body: str
    target_repo_id: str | None = None


class MetaAgentResult(BaseModel):
    """Structured output from the meta-agent pass.

    ``extraction_drafts``, ``alignment_drafts``, and ``todo_drafts`` are
    each clipped to ``MAX_PROPOSALS`` by ``run_meta_agent`` before
    returning.

    ``todo_drafts`` are per-repo tickets to resolve outstanding
    ``TODO`` / ``FIXME`` markers found in a repo clone — each sets
    ``target_repo_id`` to the repo the marker lives in (routed to that
    repo's board, exactly like ``alignment_drafts``).
    """

    updated_memory: str = ""
    extraction_drafts: list[DraftProposal] = Field(default_factory=list)
    alignment_drafts: list[DraftProposal] = Field(default_factory=list)
    todo_drafts: list[DraftProposal] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Available periodic-workflow catalogue (injected into the prompt)
# ---------------------------------------------------------------------------

# Periodic kinds a repo can opt into via a presence file (excludes the
# cross-repo/global-only ones like meta / timeout_escalation / trace_health).
_PER_REPO_PERIODIC_KINDS = frozenset({"llm_agent", "schedule_only"})


def _available_periodic_catalog() -> str:
    """Markdown list of every per-repo periodic workflow + its one-line
    purpose, for the meta prompt's ``<available-periodic-workflows>`` block.

    Names come from the periodic_loader kind map (single source of truth);
    descriptions are read from each ``agent_definitions/periodic/<name>.yaml``
    when present, else a generic fallback for the prompt-less schedule/
    schedule tasks.
    """
    from ..agents.periodic_loader import _BUILTIN_KINDS

    defs_dir = agent_definitions_dir() / "periodic"
    lines: list[str] = []
    for name, kind in _BUILTIN_KINDS.items():
        if kind not in _PER_REPO_PERIODIC_KINDS:
            continue
        desc = ""
        f = defs_dir / f"{name}.yaml"
        if f.is_file():
            try:
                raw = _yaml.safe_load(f.read_text(encoding="utf-8")) or {}
                desc = str(raw.get("description") or "").strip().split("\n")[0].strip()
            except Exception:  # noqa: BLE001 — best-effort catalogue
                desc = ""
        lines.append(f"- `{name}`: {desc or '(periodic schedule task)'}")
    lines.append("")
    lines.append(
        "**Rule**: the names above are the ONLY valid built-in periodic names. "
        "A presence file for any other name is a *bespoke* agent and MUST "
        "include a `system_prompt:` field. Name-only files for unknown names "
        "are silently rejected by the loader."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Deterministic ground-truth blocks (injected into the prompt)
#
# The meta-agent reliably confabulates surveys it never runs (it has, on
# observed passes, produced its whole output in a single LLM turn with zero
# tool calls — narrating "let me grep…" without grepping). So the facts that
# MUST NOT be missed are computed deterministically here and injected, turning
# discovery the LLM skips into judgement it can't avoid.
# ---------------------------------------------------------------------------


def _git_grep(clone: Path, args: list[str], *, timeout: int = 60) -> str:
    """Run ``git -C <clone> grep <args>`` and return stdout.

    Returns ``""`` on any failure or no-match (git grep exits 1 when there
    are no matches). Read-only, fixed argv (no shell, no untrusted input).
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "-C", str(clone), "grep", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001 — best-effort; never crash the pass
        return ""
    return proc.stdout


def _has_buildout_placeholder(clone: Path) -> bool:
    """True when a clone still ships build-out placeholders (skeleton lib).

    Matches the exact ``TODO(build-out)`` marker convention (e.g. in
    robotsix-board's placeholder ``static/board.{js,css}``).  Excludes
    agent definitions (prompts that instruct agents to *skip* the marker),
    tests (fixtures), and meta tooling (this file's own docstring) so that
    repos like mill are not misflagged as skeletons.
    """
    return bool(
        _git_grep(
            clone,
            [
                "-lF",
                "TODO(build-out)",
                "--",
                ":!agent_definitions/",
                ":!tests/",
                ":!src/robotsix_mill/meta/",
            ],
            timeout=30,
        ).strip()
    )


def _robotsix_deps_of(clone: Path) -> tuple[str, set[str]]:
    """Return ``(package_name, {robotsix-* dependency names})`` for a clone.

    Parses ``pyproject.toml`` ``[project]`` dependencies + optional
    groups + PEP 735 ``[dependency-groups]``. Best-effort: a
    missing/unparsable file yields the clone name and no deps. Names are
    lowercased for stable matching.
    """
    import re
    import tomllib

    pp = clone / "pyproject.toml"
    if not pp.is_file():
        return clone.name.lower(), set()
    try:
        data = tomllib.loads(pp.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — best-effort
        return clone.name.lower(), set()
    proj = data.get("project", {}) or {}
    pkg = str(proj.get("name", clone.name)).lower()
    dep_strs = list(proj.get("dependencies", []) or [])
    for grp in (proj.get("optional-dependencies", {}) or {}).values():
        dep_strs.extend(grp or [])
    # PEP 735 dependency-groups live at the top level, not under [project].
    for grp in (data.get("dependency-groups", {}) or {}).values():
        dep_strs.extend(grp or [])
    found = {
        m.group(1).lower()
        for d in dep_strs
        if (m := re.match(r"\s*([A-Za-z0-9._-]+)", str(d)))
        and m.group(1).lower().startswith("robotsix-")
    }
    return pkg, found


def _cross_repo_adoption(repo_clones: dict[str, Path]) -> str:
    """Deterministic ``<cross-repo-adoption>`` block.

    For every ``robotsix-*`` package that is depended on by at least one
    repo (i.e. a shared library), list which repos consume it and which
    do NOT — plus a ``[SKELETON]`` flag for libs that still ship
    build-out placeholders. Lets the agent judge adoption gaps (an
    un-migrated consumer that should adopt a built-out lib) instead of
    rediscovering dependency graphs with unreliable tools.
    """
    names: dict[str, str] = {}  # repo_id -> declared package name (lowercased)
    deps: dict[str, set[str]] = {}  # repo_id -> {robotsix-* dep names}
    for repo_id, clone in repo_clones.items():
        names[repo_id], deps[repo_id] = _robotsix_deps_of(clone)

    pkg_to_repo = {names[r]: r for r in repo_clones}
    consumers: dict[str, set[str]] = {}  # lib repo_id -> consumer repo_ids
    for r, ds in deps.items():
        for d in ds:
            lib = pkg_to_repo.get(d)
            if lib and lib != r:
                consumers.setdefault(lib, set()).add(r)

    # Report a repo as a library if it is consumed by ≥1 other repo OR it is
    # a skeleton (a lib mid-build-out with no consumers yet, e.g. board).
    all_repos = set(repo_clones)
    lib_ids = set(consumers) | {
        r for r in repo_clones if _has_buildout_placeholder(repo_clones[r])
    }
    if not lib_ids:
        return "(no shared robotsix-* libraries detected across the clones)"

    lines: list[str] = []
    for lib in sorted(lib_ids):
        cons = consumers.get(lib, set())
        non = sorted(all_repos - {lib} - cons)
        skel = (
            " [SKELETON: still ships build-out placeholders — needs BUILD-OUT"
            " before any consumer can migrate; file a build-out draft on this"
            " lib to port in the real implementation from whichever repo still"
            " owns it]"
            if _has_buildout_placeholder(repo_clones[lib])
            else ""
        )
        lines.append(
            f"- `{lib}` (pkg `{names[lib]}`){skel}: "
            f"consumed by [{', '.join(sorted(cons)) or '—'}]; "
            f"NOT consumed by [{', '.join(non) or '—'}]"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_meta_agent(
    *,
    settings: Settings,
    memory: str = "",
    recent_proposals: str = "",
    repo_clones: dict[str, Path],
) -> MetaAgentResult:
    """Run the cross-repo meta-agent pass.

    Receives all registered repo clones, surveys each one, and returns
    a structured ``MetaAgentResult`` with extraction and alignment
    draft proposals plus the updated memory ledger.

    The agent is built directly (not via ``run_periodic_agent``)
    because its result shape — separate ``extraction_drafts`` /
    ``alignment_drafts`` lists — does not fit the
    ``draft_titles`` / ``draft_bodies`` / ``gap_ids`` convention that
    ``run_periodic_agent`` clips in step 7.

    Args:
        settings: Application configuration — model names
            (``audit_model``), retry parameters, and tool paths.
        memory: The agent's memory ledger as a Markdown string.
            Defaults to ``""`` (the agent starts a fresh ledger).
        recent_proposals: Prior proposals string from the pass runner.
        repo_clones: Mapping of ``repo_id`` → ``Path`` for every
            registered repository clone.  Empty dict → early return
            with ``MetaAgentResult(updated_memory=memory)``.

    Returns:
        A ``MetaAgentResult`` with extraction and alignment drafts
        each clipped to ``MAX_PROPOSALS`` (10), plus the updated
        memory ledger.
    """
    if not repo_clones:
        return MetaAgentResult(updated_memory=memory)

    # ------------------------------------------------------------------
    # Resolve clone paths — every clone is both the "primary" root
    # (first entry) and in ``extra_roots`` (all entries) so the agent
    # can reach files in every repo.
    # ------------------------------------------------------------------
    clone_paths = list(repo_clones.values())
    repo_dir = clone_paths[0]
    extra_roots = clone_paths

    # ------------------------------------------------------------------
    # Build tools: explore (with extra_roots) + read-only fs tools
    # (filtered to read_file / list_dir — no run_command).
    # ------------------------------------------------------------------
    from ..agents.explore import make_repo_scoped_explore_tool
    from ..agents.fs_tools import build_fs_tools

    ro = [
        t
        for t in build_fs_tools(
            repo_dir,
            settings,
            extra_roots=extra_roots,
        )
        if t.__name__ in ("read_file", "list_dir")
    ]

    # Repo-scoped explore: the agent picks the target repo per call, so a
    # survey of any repo resolves against THAT clone (not the first one).
    tools = [make_repo_scoped_explore_tool(settings, repo_clones)]
    tools.extend(ro)

    # ------------------------------------------------------------------
    # Build the agent via build_agent (not build_agent_from_definition —
    # no YAML-model field dependency, no overlay support).
    # ------------------------------------------------------------------
    from pydantic_ai import PromptedOutput

    from ..agents.base import build_agent, _safe_close

    agent = build_agent(
        settings,
        system_prompt=SYSTEM_PROMPT,
        output_type=PromptedOutput(MetaAgentResult),
        tools=tools,
        web_knowledge=False,
        report_issue=False,
        read_ticket=True,
        reply_to_thread=False,
        close_thread=False,
        ask_user=False,
        # No per-agent model decision: cross-repo synthesis runs on the
        # normal/default model (llmio resolves the tier per backend), not the
        # cheap flash tier the audit_model override used to force.
        level=2,
        name="meta",
    )

    # ------------------------------------------------------------------
    # Construct the prompt
    # ------------------------------------------------------------------
    from ..agents.prompt_blocks import section
    from .todo_scan import format_outstanding_todos, scan_outstanding_todos

    clone_lines = [
        f"- `{repo_id}` → `{clone_path}`" for repo_id, clone_path in repo_clones.items()
    ]
    clone_listing = "\n".join(clone_lines)

    prompt = recent_proposals
    prompt += section("memory", memory or "(empty — start a new ledger)")
    prompt += section("repo-clones", clone_listing)
    # Hand the agent the full periodic-workflow catalogue so it can check each
    # repo for missing-but-valuable workflows without rediscovering the list.
    prompt += section("available-periodic-workflows", _available_periodic_catalog())
    # Deterministic ground-truth blocks: discovery the agent reliably skips,
    # pre-computed so it only has to judge (see the helpers' docstrings).
    _todo_scan = scan_outstanding_todos(repo_clones)
    prompt += section(
        "outstanding-todos",
        format_outstanding_todos(
            _todo_scan.markers,
            truncated_repos=_todo_scan.truncated_repos,
            global_truncated=_todo_scan.global_truncated,
        ),
    )
    prompt += section("cross-repo-adoption", _cross_repo_adoption(repo_clones))
    prompt += "\n\nPerform the cross-repo analysis and return your result."

    # ------------------------------------------------------------------
    # Run with retry
    # ------------------------------------------------------------------
    from ..agents.retry import run_agent

    try:
        result = run_agent(
            agent,
            lambda h: h.run_sync(prompt),
            what="meta-agent",
        )
    finally:
        _safe_close(agent)

    # ------------------------------------------------------------------
    # Clip and return
    # ------------------------------------------------------------------
    out: MetaAgentResult = result.output
    out.extraction_drafts = out.extraction_drafts[:MAX_PROPOSALS]
    out.alignment_drafts = out.alignment_drafts[:MAX_PROPOSALS]
    out.todo_drafts = out.todo_drafts[:MAX_PROPOSALS]
    return out
