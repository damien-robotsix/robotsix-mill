"""The audit agent: meta-audit to identify gaps in quality/security
tooling coverage.

Seam: tests monkeypatch ``run_audit_agent``. Structured output so
the runner has a clear result to work with.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from ..config import Settings

# Re-export SYSTEM_PROMPT for tests (loaded from YAML without env-var resolution)
import yaml as _yaml
_SYSPROMPT_PATH = Path(__file__).parent.parent.parent.parent / "agent_definitions" / "periodic" / "audit.yaml"
SYSTEM_PROMPT: str = _yaml.safe_load(_SYSPROMPT_PATH.read_text())["system_prompt"]


MAX_GAPS = 5


class AuditResult(BaseModel):
    updated_memory: str = ""
    draft_titles: list[str] = Field(default_factory=list)
    draft_bodies: list[str] = Field(default_factory=list)
    gap_ids: list[str] = Field(default_factory=list)


def _load_overlay(settings: Settings, board_id: str) -> str:
    """Return the per-repo audit overlay Markdown, or ``""`` when none.

    Overlays live at ``<data_dir>/<board_id>/agent_overlays/audit.md``
    and let an operator inject repo-specific guidance into the audit
    prompt without touching the shipped YAML or the repo's own code.
    """
    if not board_id:
        return ""
    path = settings.data_dir / board_id / "agent_overlays" / "audit.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def run_audit_agent(
    *,
    settings: Settings,
    memory: str = "",
    recent_proposals: str = "",
    repo_dir=None,
    board_id: str = "",
) -> AuditResult:
    """Run the meta-audit pass.

    Audits the repository through two complementary lenses —
    codebase health / maintainability (lens A, by reading the actual
    code) and tooling / security coverage (lens B, using
    ``web_research`` for external best practices) — and returns a
    structured ``AuditResult`` with draft tickets.  For recurring
    quality dimensions the agent proposes dedicated standing
    checkers rather than emitting per-instance remediation tickets.

    When ``repo_dir`` is provided, the agent gets filesystem tools
    (``read_file``, ``list_dir``) and the ``explore`` scout tool so
    it can inspect the actual codebase.  Without ``repo_dir`` the
    agent runs in a read-only reasoning mode (no repo access).

    The agent is constructed via :func:`~.base.build_agent` with the
    role-specific ``SYSTEM_PROMPT``, structured output type
    ``PromptedOutput(AuditResult)`` (for provider compatibility),
    ``web=True`` (for the ``web_research`` sub-agent tool),
    ``report_issue=False`` (the agent emits drafts through its
    structured output instead), and
    ``model_name=settings.audit_model``.

    Execution is wrapped in :func:`~.retry.call_with_retry`, which
    handles transient network/model failures (exponential backoff:
    2s base, 30s cap) and ``UsageLimitExceeded`` rate-limit errors
    (30s base, 120s cap with provider fallback after
    ``settings.rate_limit_fallback_retries`` consecutive failures).

    .. note::

        The tool-building pipeline (``make_explore_tool`` + filtered
        ``build_fs_tools`` → ``build_agent``) is duplicated verbatim
        in :func:`~.health.run_health_agent`.  This is tracked under
        the ``audit_health_duplication`` hotspot (see
        :mod:`~.agent_check`).  Both agents inspect overlapping
        codebase-health dimensions; changes to the pipeline should
        be made in both places until a shared builder is extracted.

    Args:
        settings: Application configuration — model names
            (``audit_model``), retry parameters, forge URL, and
            tool paths.
        memory: The agent's memory ledger as a Markdown string.
            Defaults to ``""`` (the agent starts a fresh ledger).
        repo_dir: Optional path to the local repository clone.
            When not ``None``, enables the ``explore``,
            ``read_file``, and ``list_dir`` tools.

    Returns:
        An ``AuditResult`` with draft titles, bodies, and gap IDs
        clipped to ``MAX_GAPS`` (5) entries, plus the updated memory
        ledger.
    """
    from pydantic_ai import PromptedOutput

    from .yaml_loader import load_agent_definition
    from .base import build_agent_from_definition, _safe_close

    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent / "agent_definitions" / "periodic" / "audit.yaml"
    )

    tools: list = []
    if repo_dir is not None:
        from .explore import make_explore_tool
        from .fs_tools import build_fs_tools
        from .jscpd_tool import make_jscpd_tool

        ro = [
            t for t in build_fs_tools(repo_dir, settings)
            if t.__name__ in ("read_file", "list_dir", "run_command")
        ]
        tools = [make_explore_tool(settings, repo_dir), make_jscpd_tool(repo_dir), *ro]

    overlay = _load_overlay(settings, board_id)
    system_prompt = definition.system_prompt
    if overlay:
        system_prompt = f"{system_prompt}\n\n{overlay}\n"

    agent = build_agent_from_definition(
        settings, definition, tools=tools,
        model_name=definition.model or settings.audit_model,
        system_prompt=system_prompt,
    )
    from .prompt_blocks import section
    forge_url = settings.forge_remote_url or "(not configured)"
    prompt = (
        f"{recent_proposals}"
        + section("forge-remote-url", forge_url) + "\n\n"
        + section("memory", memory or "(empty — start a new ledger)") + "\n\n"
        + "Perform the audit and return your result."
    )
    from .retry import call_with_retry

    try:
        result = call_with_retry(
            lambda: agent.run_sync(prompt), settings=settings, what="audit"
        )
    finally:
        _safe_close(agent)
    result.output.draft_titles = result.output.draft_titles[:MAX_GAPS]
    result.output.draft_bodies = result.output.draft_bodies[:MAX_GAPS]
    result.output.gap_ids = result.output.gap_ids[:MAX_GAPS]
    return result.output
