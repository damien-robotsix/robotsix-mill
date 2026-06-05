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

from ..config import Settings
from ..runners.pass_runner import ProposedActionItem

# ---------------------------------------------------------------------------
# Load the static system prompt from the YAML definition
# ---------------------------------------------------------------------------

import yaml as _yaml

_SYSPROMPT_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "agent_definitions"
    / "periodic"
    / "meta.yaml"
)
SYSTEM_PROMPT: str = _yaml.safe_load(_SYSPROMPT_PATH.read_text())["system_prompt"]


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
    proposed_actions: list[ProposedActionItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Available periodic-workflow catalogue (injected into the prompt)
# ---------------------------------------------------------------------------

# Periodic kinds a repo can opt into via a presence file (excludes the
# cross-repo/global-only ones like meta / timeout_escalation / trace_health).
_PER_REPO_PERIODIC_KINDS = frozenset({"llm_agent", "schedule_only", "maintenance"})


def _available_periodic_catalog() -> str:
    """Markdown list of every per-repo periodic workflow + its one-line
    purpose, for the meta prompt's ``<available-periodic-workflows>`` block.

    Names come from the periodic_loader kind map (single source of truth);
    descriptions are read from each ``agent_definitions/periodic/<name>.yaml``
    when present, else a generic fallback for the prompt-less schedule/
    maintenance tasks.
    """
    from ..agents.periodic_loader import _BUILTIN_KINDS

    defs_dir = (
        Path(__file__).parent.parent.parent.parent / "agent_definitions" / "periodic"
    )
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
        lines.append(f"- `{name}`: {desc or '(periodic schedule/maintenance task)'}")
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
    from ..agents.explore import make_explore_tool
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

    tools = [make_explore_tool(settings, repo_dir, extra_roots=extra_roots)]
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
        model_name=None,
        name="meta",
    )

    # ------------------------------------------------------------------
    # Construct the prompt
    # ------------------------------------------------------------------
    from ..agents.prompt_blocks import section

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
    prompt += "\n\nPerform the cross-repo analysis and return your result."

    # ------------------------------------------------------------------
    # Run with retry
    # ------------------------------------------------------------------
    from ..agents.retry import run_agent

    try:
        result = run_agent(
            agent,
            lambda h: h.run_sync(prompt),
            settings=settings,
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
