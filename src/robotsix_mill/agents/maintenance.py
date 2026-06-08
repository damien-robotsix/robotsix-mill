"""Maintenance agent — performs operational actions (create repo,
fork repo, cross-repo investigation) directly, bypassing the
implement pipeline.

The agent module provides the ``MaintenanceResult`` model, tool
builders for forge operations and reporting, and the
``run_maintenance_agent`` entry point called by
:class:`~robotsix_mill.stages.maintenance.MaintenanceStage`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel

from ..config import Settings
from ..core.models import Ticket
from ..stages.base import StageContext

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


class MaintenanceResult(BaseModel):
    """Structured output from the maintenance agent."""

    success: bool
    note: str | None = None


# ---------------------------------------------------------------------------
# Tool builders
# ---------------------------------------------------------------------------


def make_create_repo_tool(
    settings: Settings, ctx: StageContext, ticket_description: str
) -> Callable[..., str]:
    """Return the ``create_repo`` closure bound to *settings*, *ctx*, and
    *ticket_description*.

    The tool calls :meth:`Forge.create_repo`, then
    :func:`~robotsix_mill.repo_scaffold.run_repo_scaffold` to scaffold an
    initial commit, register the repo in ``config/repos.yaml``, and file a
    build-out ticket.  Returns structured metadata as a JSON string, or an
    error message prefixed with ``create_repo:`` on failure.
    """

    def create_repo(
        name: str,
        owner: str,
        private: bool,
        description: str,
        language: str = "python",
    ) -> str:
        """Create a new repository under *owner* and return its metadata.

        Args:
            name: Repository name.
            owner: Owner (user or organization).
            private: Whether the repo is private.
            description: Short description.
            language: Project language (default ``"python"``).

        Returns:
            JSON string with success, id, name, clone_url, html_url,
            and note, or an error message starting with ``create_repo:``.
        """
        from ..forge import get_forge, NotConfiguredError
        from ..repo_scaffold import run_repo_scaffold
        from ..core.states import State

        try:
            forge = get_forge(settings)
        except NotConfiguredError:
            return "create_repo: repo creation is not configured"
        except Exception as exc:
            return f"create_repo: {exc!r}"

        params = {
            "name": name,
            "owner": owner,
            "private": private,
            "description": description,
            "language": language,
        }

        try:
            repo_info = forge.create_repo(
                name=name,
                owner=owner,
                private=private,
                description=description,
            )
        except Exception as exc:
            return f"create_repo: {exc!r}"

        try:
            outcome = run_repo_scaffold(
                settings, forge, ctx, params, ticket_description
            )
        except Exception as exc:
            return f"create_repo: scaffold failed: {exc!r}"

        if outcome.next_state != State.DONE:
            return f"create_repo: scaffold returned {outcome.next_state.value}: {outcome.note}"

        return json.dumps(
            {
                "success": True,
                "id": repo_info.id,
                "name": repo_info.name,
                "clone_url": repo_info.clone_url,
                "html_url": repo_info.html_url,
                "note": outcome.note,
            }
        )

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="create_repo",
            description="Create a new repository, scaffold it, and return its metadata.",
            category="reporting",
            parameters={
                "name": "str",
                "owner": "str",
                "private": "bool",
                "description": "str",
                "language": "str (optional, default 'python')",
            },
        )
    )
    return create_repo


def make_fork_repo_tool(settings: Settings) -> Callable[..., str]:
    """Return the ``fork_repo`` closure bound to *settings*.

    The tool calls :meth:`Forge.fork_repo` and returns structured
    metadata (id, name, clone_url, html_url) as a JSON string, or
    an error message prefixed with ``fork_repo:`` on failure.
    """

    def fork_repo(
        source_owner: str,
        source_repo: str,
        target_namespace: str | None = None,
    ) -> str:
        """Fork *source_owner/source_repo* and return the new fork's metadata.

        Args:
            source_owner: Owner of the source repository.
            source_repo: Name of the source repository.
            target_namespace: Optional organization/namespace to fork
                into (defaults to the authenticated user).

        Returns:
            JSON string with id, name, clone_url, html_url, or an
            error message starting with ``fork_repo:``.
        """
        from ..forge import get_forge, NotConfiguredError

        try:
            forge = get_forge(settings)
            info = forge.fork_repo(
                source_owner=source_owner,
                source_repo=source_repo,
                target_namespace=target_namespace,
            )
        except NotConfiguredError:
            return "fork_repo: repo forking is not configured"
        except Exception as exc:
            return f"fork_repo: {exc!r}"
        return json.dumps(
            {
                "id": info.id,
                "name": info.name,
                "clone_url": info.clone_url,
                "html_url": info.html_url,
            }
        )

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="fork_repo",
            description="Fork a repository and return the new fork's metadata.",
            category="reporting",
            parameters={
                "source_owner": "str",
                "source_repo": "str",
                "target_namespace": "str (optional)",
            },
        )
    )
    return fork_repo


def make_investigate_tool(settings: Settings) -> Callable[..., str]:
    """Return the ``investigate`` stub bound to *settings*.

    The stub returns a clear "not yet implemented" error so the agent
    loop doesn't crash before the real cross-repo investigation is
    implemented.
    """

    def investigate(question: str, repo_url: str) -> str:
        """Investigate a question across repositories (stub).

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


def make_post_findings_tool(settings: Settings, agent_name: str) -> Callable[..., str]:
    """Return the ``post_findings`` closure — a thin wrapper around
    ``post_comment`` with a domain-appropriate name for the
    maintenance agent.

    The underlying ``post_comment`` tool is idempotent on (ticket, body)
    so a retrying agent doesn't spam duplicates.
    """

    from .post_comment import make_post_comment_tool

    _post_comment: Callable[[str], str] = make_post_comment_tool(settings, agent_name)

    def post_findings(body: str) -> str:
        """Post your findings as a Markdown comment on the current ticket.

        This is your primary output channel — use it to report what
        action was taken, what was created, URLs, metadata, and any
        errors encountered.

        Args:
            body: Markdown report body.

        Returns:
            A status string with the comment id, or an error message.
        """
        return _post_comment(body)

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="post_findings",
            description=(
                "Post findings as a Markdown comment on the current "
                "ticket. Use to report operational results: created "
                "repo URLs, fork metadata, investigation conclusions."
            ),
            category="reporting",
            parameters={"body": "str (Markdown report)"},
        )
    )

    return post_findings


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_maintenance_agent(ticket: Ticket, ctx: StageContext) -> MaintenanceResult:
    """Load the maintenance agent definition, build its tool set, run the
    agent loop, and return a structured :class:`MaintenanceResult`.

    Called by :meth:`MaintenanceStage.run`.
    """
    from .base import build_agent_from_definition, _safe_close
    from .retry import run_agent
    from .yaml_loader import load_agent_definition

    # 1. Load the YAML definition
    definition = load_agent_definition(
        Path(__file__).parent.parent.parent.parent
        / "agent_definitions"
        / "maintenance.yaml"
    )

    # 2. Read the ticket draft (needed for tool + prompt)
    ws = ctx.service.workspace(ticket)
    draft = ws.read_description().strip()

    # 3. Build the tool list
    tools: list[Any] = []
    tools.append(make_create_repo_tool(ctx.settings, ctx, draft))
    tools.append(make_fork_repo_tool(ctx.settings))
    tools.append(make_investigate_tool(ctx.settings))
    tools.append(make_post_findings_tool(ctx.settings, "maintenance"))

    # 4. Build the agent
    agent = build_agent_from_definition(
        ctx.settings,
        definition,
        tools=tools,
    )

    # 5. Build the user prompt from the ticket
    user_prompt = f"# Title\n{ticket.title}\n\n# Draft\n{draft}"

    # 6. Run the agent
    try:
        result = run_agent(
            agent,
            lambda h: h.run_sync(user_prompt),
            settings=ctx.settings,
            what="maintenance",
        )
    finally:
        _safe_close(agent)

    # 7. Coerce the output
    if isinstance(result.output, MaintenanceResult):
        return result.output
    if isinstance(result.output, dict):
        return MaintenanceResult(**result.output)
    return MaintenanceResult(
        success=False,
        note="Agent produced no structured output",
    )
