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

from pydantic import BaseModel, field_validator

from ..config import Settings
from ..core.models import Ticket
from ..core.states import State
from ..stages.base import StageContext

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Command allowlist for the maintenance agent's run_command
# ---------------------------------------------------------------------------

# Commands the maintenance agent is allowed to run via run_command.
# Compound commands (&&, ||, |, ;) are split into segments; every
# segment's first word must be in this set or the whole command is
# rejected BEFORE reaching the sandbox.
_MAINTENANCE_SAFE_COMMANDS: frozenset[str] = frozenset(
    {
        "git",
        "grep",
        "ls",
        "find",
        "cat",
        "head",
        "tail",
        "wc",
        "sort",
        "uniq",
        "diff",
        "sed",
        "awk",
        "cut",
        "tr",
        "xargs",
        "echo",
        "dirname",
        "basename",
        "realpath",
        "readlink",
        "stat",
        "file",
        "du",
        "tree",
        "cd",
    }
)


def _validate_command(command: str) -> str | None:
    """Return an error string if *command* contains a non-allowlisted
    executable, or ``None`` if every segment passes.

    Splits on ``&&``, ``||``, ``|``, ``;`` and checks the first
    whitespace-delimited word of each segment against
    ``_MAINTENANCE_SAFE_COMMANDS``.  Segments that are pure ``cd``
    (with or without a directory argument) are skipped — this lets the
    agent navigate between repo subdirectories via ``cd <dir> && ...``
    or ``cd <dir> || ...`` chains.
    """
    import re

    # Split on shell separators (capturing parens keep the separators
    # in the result list, but we only examine every other element —
    # the actual command segments).
    segments = re.split(r"(&&|\|\||\||;)", command)
    for i in range(0, len(segments), 2):
        segment = segments[i].strip()
        if not segment:
            continue
        # A pure "cd" segment (with or without target dir) is always
        # allowed — it just changes the working directory.
        if segment == "cd" or segment.startswith("cd "):
            continue
        # Extract the first word (the executable)
        first_word = segment.split()[0] if segment.split() else ""
        if not first_word:
            continue
        if first_word not in _MAINTENANCE_SAFE_COMMANDS:
            return (
                f"command rejected: '{first_word}' is not in the "
                f"maintenance agent's safe-command allowlist. "
                f"Allowed commands: {', '.join(sorted(_MAINTENANCE_SAFE_COMMANDS))}"
            )
    return None


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


class MaintenanceResult(BaseModel):
    """Structured output from the maintenance agent."""

    success: bool
    note: str | None = None
    redirect_to: State | None = None

    @field_validator("redirect_to", mode="before")
    @classmethod
    def _coerce_redirect_to(cls, v: str | State | None) -> State | None:
        """Coerce the raw string ``"ready"`` or ``"draft"`` into the
        corresponding :class:`State` enum member, and reject anything
        else that isn't already a valid ``State`` or ``None``.

        While ``State`` is a ``StrEnum`` and Pydantic v2 auto-coerces
        in most cases, the explicit before-validator guards against
        edge cases where the agent returns a raw ``dict`` with
        ``"redirect_to": "ready"``.
        """
        if v is None:
            return None
        if isinstance(v, State):
            if v not in (State.READY, State.DRAFT):
                raise ValueError(
                    f"redirect_to must be State.READY or State.DRAFT, got {v}"
                )
            return v
        if not isinstance(v, str):
            raise ValueError(
                f"redirect_to must be a string or State, got {type(v).__name__}"
            )
        try:
            result = State(v)
        except ValueError:
            raise ValueError(
                f"redirect_to must be 'ready' or 'draft', not {v!r}"
            ) from None
        if result not in (State.READY, State.DRAFT):
            raise ValueError(f"redirect_to must be 'ready' or 'draft', not {v!r}")
        return result


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


def make_clone_repo_tool(
    settings: Settings, workspace_root: Path
) -> Callable[..., str]:
    """Return the ``clone_repo`` closure bound to *settings* and
    *workspace_root*.

    The closure clones a target git repository into the workspace so
    the filesystem tools (read_file, list_dir, run_command, explore)
    can inspect it locally.  Uses the existing
    :func:`~robotsix_mill.vcs.git_ops.clone` utility.
    """

    def clone_repo(url: str, branch: str = "main") -> str:
        """Clone a git repository into the local workspace.

        Clones the given *url* into the workspace so the filesystem
        tools (read_file, list_dir, run_command, explore) can inspect
        it.  If a previous clone exists at the target path, it is
        removed first.

        Args:
            url: Git HTTPS/SSH URL of the repository to clone.
            branch: Branch to clone (default ``"main"``).

        Returns:
            Status string with the local clone path on success, or
            an error message starting with ``clone_repo:``.
        """
        import shutil

        from ..forge.auth import github_token
        from ..vcs.git_ops import clone as git_clone

        target = workspace_root / "repo"

        # Remove previous clone if it exists (single-repo model)
        if target.exists():
            shutil.rmtree(target)

        # Obtain auth token (same pattern as every production call site)
        try:
            token = github_token(settings)
        except RuntimeError:
            token = None

        try:
            git_clone(url, target, branch, token=token)
        except Exception as exc:
            # Redact defensively even though git_ops.clone already
            # sanitizes its own CalledProcessError: any OTHER exception
            # repr that embeds the authed URL must not reach the model
            # transcript / Langfuse.
            from ..vcs.git_ops import redact_credentials

            return f"clone_repo: {redact_credentials(repr(exc))}"

        return f"Cloned {url} (branch={branch}) into {target}"

    from .tool_registry import ToolInfo, ToolRegistry

    ToolRegistry.register(
        ToolInfo(
            name="clone_repo",
            description=(
                "Clone a target git repository into the local workspace "
                "so the filesystem tools (read_file, list_dir, "
                "run_command, explore) can inspect it."
            ),
            category="fs",
            parameters={
                "url": "str",
                "branch": "str (optional, default 'main')",
            },
        )
    )
    return clone_repo


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

    # 3. Create a temporary workspace directory for the clone
    import tempfile

    # 4. Build the tool list (must be inside the tempdir context so
    #    clone_repo can populate the workspace)
    with tempfile.TemporaryDirectory(prefix="maintenance_") as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        # Must mirror make_clone_repo_tool's ``workspace_root / "repo"``
        # clone target so the investigation tools can read the clone.
        clone_dir = tmpdir / "repo"
        tools: list[Any] = []
        tools.append(make_create_repo_tool(ctx.settings, ctx, draft))
        tools.append(make_fork_repo_tool(ctx.settings))
        tools.append(make_clone_repo_tool(ctx.settings, tmpdir))
        tools.append(make_post_findings_tool(ctx.settings, "maintenance"))

        # Determine the effective root for investigation tools.
        # When investigation_workspace is set, tools are scoped to
        # that pre-populated directory; otherwise fall back to the
        # ticket's own workspace repo dir.
        investigation_root = (
            ctx.settings.investigation_workspace
            if ctx.settings.investigation_workspace is not None
            else ws.repo_dir
        )

        # Read-only filesystem tools (read_file, list_dir, run_command)
        from .fs_tools import build_fs_tools

        all_fs = build_fs_tools(
            investigation_root, ctx.settings, extra_roots=[clone_dir]
        )
        ro_fs = {
            t.__name__: t
            for t in all_fs
            if t.__name__ in ("read_file", "list_dir", "run_command")
        }
        tools.append(ro_fs["read_file"])
        tools.append(ro_fs["list_dir"])

        # Wrap run_command with the maintenance allowlist
        _sandbox_run = ro_fs["run_command"]

        def run_command(command: str) -> str:
            """Run a sandboxed read-only shell command in the investigation workspace.

            Only safe, read-only commands are allowed (git, grep, ls, find,
            cat, head, tail, wc, sort, uniq, diff, ...).  Write-capable or
            destructive commands are rejected before execution.
            """
            err = _validate_command(command)
            if err is not None:
                return err
            return _sandbox_run(command)

        tools.append(run_command)

        # Explore / parallel_explore tools scoped to investigation_root
        from .explore import make_explore_tool, make_parallel_explore_tool

        tools.append(
            make_explore_tool(ctx.settings, investigation_root, extra_roots=[clone_dir])
        )
        tools.append(
            make_parallel_explore_tool(
                ctx.settings, investigation_root, extra_roots=[clone_dir]
            )
        )

        # 5. Build the agent. board_id wires the report_issue tool to the
        # ticket's own board — without it the tool dies with "board_id is
        # required" the moment the agent tries to file a blocking issue
        # (post-board-migration regression seen live on ticket 590c).
        agent = build_agent_from_definition(
            ctx.settings,
            definition,
            tools=tools,
            repo_dir=investigation_root,
            board_id=ticket.board_id,
        )

        # 6. Build the user prompt from the ticket. Include the board and
        # the repo's REAL clone URL — without it the agent guesses the
        # remote from the draft text (live case: guessed
        # ``robotsix/mill.git`` for damien-robotsix/robotsix-mill, watched
        # three clones fail, and misdiagnosed "network unavailable").
        from ..forge.auth import _resolve_remote_url

        remote_url = _resolve_remote_url(ctx.settings, ctx.repo_config)
        repo_context = f"# Board\n{ticket.board_id}\n"
        if remote_url:
            repo_context += (
                f"\n# Repository clone URL\n{remote_url}\n"
                "(pass this EXACT url to clone_repo — do not guess remotes)\n"
            )
        user_prompt = f"{repo_context}\n# Title\n{ticket.title}\n\n# Draft\n{draft}"

        # 7. Run the agent — with an explicit request cap (the implicit
        # pydantic-ai default of 50 is what blocked the data-dir audit
        # tickets with an opaque "Fatal: UsageLimitExceeded").
        from pydantic_ai.usage import UsageLimits

        limits = UsageLimits(request_limit=ctx.settings.maintenance_request_limit)
        try:
            result = run_agent(
                agent,
                lambda h: h.run_sync(user_prompt, usage_limits=limits),
                settings=ctx.settings,
                what="maintenance",
            )
        finally:
            _safe_close(agent)

        # 8. Coerce the output
        if isinstance(result.output, MaintenanceResult):
            return result.output
        if isinstance(result.output, dict):
            return MaintenanceResult(**result.output)
        return MaintenanceResult(
            success=False,
            note="Agent produced no structured output",
        )
