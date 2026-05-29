"""The copy-paste agent: deterministic clone detection and triage.

Runs jscpd (via ``detect_duplication``) to find copy-paste duplication,
triages clone pairs by severity, cross-references against the memory
ledger and ``recent-proposals``, and files one draft ticket per
high-severity clone pair.

Seam: tests monkeypatch ``run_copy_paste_agent``. Structured output so
the runner has a clear result to work with.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from ..config import Settings

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)
import yaml as _yaml

_SYSPROMPT_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "agent_definitions"
    / "periodic"
    / "copy_paste.yaml"
)
SYSTEM_PROMPT: str = _yaml.safe_load(_SYSPROMPT_PATH.read_text())["system_prompt"]


MAX_GAPS = 8


class CopyPasteResult(BaseModel):
    updated_memory: str = ""
    draft_titles: list[str] = Field(default_factory=list)
    draft_bodies: list[str] = Field(default_factory=list)
    gap_ids: list[str] = Field(default_factory=list)


def run_copy_paste_agent(
    *,
    settings: Settings,
    memory: str = "",
    recent_proposals: str = "",
    repo_dir=None,
) -> CopyPasteResult:
    """Run the copy-paste detection pass.

    Runs deterministic clone detection (jscpd via
    ``detect_duplication``), triages clone pairs by severity
    (``files × lines`` product, ≥3 files OR ≥30 duplicated lines),
    cross-references against the memory ledger and ``recent-proposals``
    for resolved/declined clones, reads clone files with ``read_file``
    to confirm genuine copy-paste, and files one draft ticket per
    high-severity clone pair.

    When ``repo_dir`` is provided, the agent gets filesystem tools
    (``read_file``, ``list_dir``), the ``explore`` scout tool, and
    the ``detect_duplication`` tool (injected at runtime via
    ``make_jscpd_tool``, following the audit pattern).

    Args:
        settings: Application configuration — model names
            (``copy_paste_model``), retry parameters, and tool paths.
        memory: The agent's memory ledger as a Markdown string.
            Defaults to ``""`` (the agent starts a fresh ledger).
        recent_proposals: Prior proposals string from pass runner.
        repo_dir: Optional path to the local repository clone.

    Returns:
        A ``CopyPasteResult`` with draft titles, bodies, and gap IDs
        clipped to ``MAX_GAPS`` (8) entries, plus the updated memory
        ledger.
    """
    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent
        / "agent_definitions"
        / "periodic"
        / "copy_paste.yaml"
    )

    tools: list = []
    if repo_dir is not None:
        from .explore import make_explore_tool
        from .fs_tools import build_fs_tools
        from .jscpd_tool import make_jscpd_tool

        ro = [
            t
            for t in build_fs_tools(repo_dir, settings)
            if t.__name__ in ("read_file", "list_dir")
        ]
        tools = [make_explore_tool(settings, repo_dir), make_jscpd_tool(repo_dir), *ro]

    from .overlays import apply_overlay, load_overlay

    system_prompt = apply_overlay(
        definition.system_prompt,
        load_overlay(repo_dir, "copy_paste"),
    )

    agent = build_agent_from_definition(
        settings,
        definition,
        tools=tools,
        model_name=definition.model or settings.copy_paste_model,
        system_prompt=system_prompt,
    )
    from .prompt_blocks import section

    prompt = (
        f"{recent_proposals}"
        + section("memory", memory or "(empty — start a new ledger)")
        + "\n\n"
        + "Run detect_duplication, triage the clone pairs, and return your findings."
    )
    from .retry import call_with_retry

    try:
        result = call_with_retry(
            lambda: agent.run_sync(prompt), settings=settings, what="copy_paste"
        )
    finally:
        _safe_close(agent)
    result.output.draft_titles = result.output.draft_titles[:MAX_GAPS]
    result.output.draft_bodies = result.output.draft_bodies[:MAX_GAPS]
    result.output.gap_ids = result.output.gap_ids[:MAX_GAPS]
    return result.output
