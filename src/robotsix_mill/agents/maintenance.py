"""Maintenance agent — performs operational actions (create repo,
fork repo, cross-repo investigation) directly, skipping the
code-implement stage.

Exports:
    :class:`MaintenanceResult` — the structured output model
    :func:`run_maintenance_agent` — entry point called by
        :class:`~robotsix_mill.stages.maintenance.MaintenanceStage`.

Tool stubs (``create_repo``, ``fork_repo``, ``investigate``) return
clear error messages so the LLM can see the tools exist but are
unavailable without crashing the agent loop.  Real implementations
are separate tickets (3–5 in the epic).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from ..config import Settings

if TYPE_CHECKING:
    from ..core.models import Ticket
    from ..stages.base import StageContext

log = logging.getLogger(__name__)


# ── Output model ─────────────────────────────────────────────────────


class MaintenanceResult(BaseModel):
    """Structured output from one maintenance-agent run.

    Mirrors the contract expected by
    :meth:`MaintenanceStage.run() <robotsix_mill.stages.maintenance.MaintenanceStage.run>`.
    """

    success: bool = False
    note: str = ""


# ── Tool stub factories ──────────────────────────────────────────────


def make_create_repo_tool(settings: Settings) -> Any:
    """Return an async ``create_repo`` stub bound to *settings*.

    The stub returns a clear "not yet implemented" error so the agent
    loop doesn't crash before the real implementation is migrated.
    """

    async def create_repo(
        name: str,
        owner: str | None = None,
        private: bool = False,
        description: str = "",
    ) -> str:
        """Create a new repository on the configured forge.

        (stub — actual implementation migrated in a separate ticket)

        Args:
            name: Repository name.
            owner: Owner or organisation (``None`` = authenticated user).
            private: Whether the repo is private.
            description: Short description.

        Returns:
            Status string.
        """
        return "create_repo: not yet implemented — pending migration ticket"

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="create_repo",
            description="Create a new repository on the configured forge (stub).",
            category="reporting",
            parameters={
                "name": "str",
                "owner": "str | None",
                "private": "bool",
                "description": "str",
            },
        )
    )

    return create_repo


def make_fork_repo_tool(settings: Settings) -> Any:
    """Return an async ``fork_repo`` stub bound to *settings*."""

    async def fork_repo(
        source_owner: str,
        source_repo: str,
        target_namespace: str | None = None,
    ) -> str:
        """Fork a repository on the configured forge.

        (stub — actual implementation migrated in a separate ticket)

        Args:
            source_owner: Owner of the source repository.
            source_repo: Name of the source repository.
            target_namespace: Organisation/namespace to fork into
                (``None`` = authenticated user's account).

        Returns:
            Status string.
        """
        return "fork_repo: not yet implemented — pending migration ticket"

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="fork_repo",
            description="Fork a repository on the configured forge (stub).",
            category="reporting",
            parameters={
                "source_owner": "str",
                "source_repo": "str",
                "target_namespace": "str | None",
            },
        )
    )

    return fork_repo


def make_investigate_tool(settings: Settings) -> Any:
    """Return an async ``investigate`` stub bound to *settings*."""

    async def investigate(
        question: str,
        repo_url: str,
    ) -> str:
        """Investigate a question across repositories.

        (stub — actual implementation migrated in a separate ticket)

        Args:
            question: The investigation question.
            repo_url: URL of the repository to investigate.

        Returns:
            Status string.
        """
        return "investigate: not yet implemented — pending migration ticket"

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="investigate",
            description="Investigate a question across repositories (stub).",
            category="reporting",
            parameters={
                "question": "str",
                "repo_url": "str",
            },
        )
    )

    return investigate


# ── Entry point ─────────────────────────────────────────────────────


def run_maintenance_agent(
    ticket: Ticket,
    ctx: StageContext,
) -> MaintenanceResult:
    """Run one maintenance-agent pass for *ticket* with *ctx*.

    Builds a pydantic-ai agent from the ``maintenance.yaml``
    definition, assembles the tool palette (exploration, read-only FS,
    action stubs + ``post_comment``), and runs the prompt synchronously.

    Returns a :class:`MaintenanceResult` whose ``.success`` and
    ``.note`` fields satisfy the
    :meth:`~robotsix_mill.stages.maintenance.MaintenanceStage.run`
    contract.
    """
    from .base import build_agent_from_definition, _safe_close
    from .post_comment import make_post_comment_tool
    from .prompt_blocks import section
    from .retry import run_agent
    from .yaml_loader import load_agent_definition

    settings: Settings = ctx.settings

    # --- load YAML definition ----------------------------------------
    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent
        / "agent_definitions"
        / "maintenance.yaml"
    )

    # --- determine repo_dir (optional) -------------------------------
    repo_dir: Path | None = None
    try:
        ws = ctx.service.workspace(ticket)
        candidate = ws.dir / "repo"
        if candidate.exists():
            repo_dir = candidate
    except Exception:
        log.debug("No workspace clone available for %s", ticket.id)

    # --- assemble tool palette ---------------------------------------
    tools: list[Any] = []

    # Exploration tool (only when a repo clone is available)
    if repo_dir is not None:
        from .explore import make_explore_tool

        tools.append(make_explore_tool(settings, repo_dir))

    # Read-only FS tools: read_file, list_dir (no write/edit/run)
    if repo_dir is not None:
        from .fs_tools import build_fs_tools

        fs_all = build_fs_tools(repo_dir, settings)
        ro_tools = [
            t
            for t in fs_all
            if t.__name__ in ("read_file", "list_dir")
        ]
        tools.extend(ro_tools)

    # Action tools: post_comment (real) + stubs
    tools.append(make_post_comment_tool(settings, agent_name="maintenance"))
    tools.append(make_create_repo_tool(settings))
    tools.append(make_fork_repo_tool(settings))
    tools.append(make_investigate_tool(settings))

    # --- build agent -------------------------------------------------
    overrides: dict[str, str] = {}
    if not definition.model:
        overrides["model_name"] = settings.model

    agent = build_agent_from_definition(
        settings,
        definition,
        tools=tools,
        repo_dir=repo_dir,
        **overrides,
    )

    # --- assemble user prompt ----------------------------------------
    forge_url = settings.forge_remote_url or "(none)"
    prompt = (
        section("ticket-id", ticket.id)
        + "\n\n"
        + section("forge-remote-url", forge_url)
        + "\n\n"
        + section("memory", "(empty — memory plumbing pending)")
        + "\n"
        + "Perform the requested maintenance action and return your result."
    )

    # --- run ---------------------------------------------------------
    try:
        result = run_agent(
            agent,
            lambda h: h.run_sync(prompt),
            settings=settings,
            what="maintenance",
        )
    finally:
        _safe_close(agent)

    out: MaintenanceResult = result.output
    return out
