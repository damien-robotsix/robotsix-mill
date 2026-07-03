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

from pydantic import BaseModel, field_validator, ConfigDict

from robotsix_mill._resources import agent_definitions_dir
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


def _scan_quoted(command: str, i: int, quote_char: str) -> tuple[list[str], int]:
    """Scan from *i* past *quote_char* through the closing quote, returning
    the characters consumed and the advanced index."""
    chars: list[str] = [quote_char]
    i += 1
    n = len(command)
    while i < n and command[i] != quote_char:
        chars.append(command[i])
        i += 1
    if i < n:
        chars.append(command[i])
        i += 1
    return chars, i


def _split_shell_separators(command: str) -> list[str]:
    """Split *command* on ``&&``, ``||``, ``|``, ``;``, respecting
    single- and double-quoted regions so that ``|`` inside quotes
    (e.g. ``grep 'error|warning'``) is not mistaken for a pipe."""
    segments: list[str] = []
    current: list[str] = []
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if ch in ("'", '"'):
            quoted, i = _scan_quoted(command, i, ch)
            current.extend(quoted)
        elif ch == "&" and i + 1 < n and command[i + 1] == "&":
            segments.append("".join(current))
            segments.append("&&")
            current = []
            i += 2
        elif ch == "|" and i + 1 < n and command[i + 1] == "|":
            segments.append("".join(current))
            segments.append("||")
            current = []
            i += 2
        elif ch in ("|", ";"):
            segments.append("".join(current))
            segments.append(ch)
            current = []
            i += 1
        else:
            current.append(ch)
            i += 1
    segments.append("".join(current))
    return segments


def _validate_command(command: str) -> str | None:
    """Return an error string if *command* contains a non-allowlisted
    executable, or ``None`` if every segment passes.

    Splits on ``&&``, ``||``, ``|``, ``;`` (respecting shell quoting
    so that ``|`` inside quotes is not treated as a pipe) and checks
    the first whitespace-delimited word of each segment against
    ``_MAINTENANCE_SAFE_COMMANDS``.  Segments that are pure ``cd``
    (with or without a directory argument) are skipped — this lets the
    agent navigate between repo subdirectories via ``cd <dir> && ...``
    or ``cd <dir> || ...`` chains.
    """
    segments = _split_shell_separators(command)
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

    model_config = ConfigDict(strict=True, extra="forbid")

    success: bool
    note: str | None = None
    redirect_to: State | None = None
    # Board id (or repo id) the ticket actually belongs to. When set,
    # the stage migrates the ticket to that board instead of blocking —
    # the fix for misrouted tickets whose change targets another repo.
    migrate_to_board: str | None = None

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
        description: str,
        private: bool | None = None,
        language: str = "python",
    ) -> str:
        """Create a new repository under *owner* and return its metadata.

        Args:
            name: Repository name.
            owner: Owner (user or organization).
            description: Short description.
            private: Whether the repo is private (defaults to config).
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
                "private": "bool (optional, defaults to repo_visibility_default config)",
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


def _build_meta_investigation_workspace(
    ctx: StageContext, ticket: Ticket, ws: Any, draft: str
) -> tuple[Path | None, list[Path], MaintenanceResult | None]:
    """Build the multi-repo investigation workspace for a meta-board ticket.

    Meta tickets are cross-repo. The maintenance agent's single-repo model
    (``clone_repo`` into a tempdir + ``investigation_root = ws.repo_dir``)
    cannot inspect more than one repo, and ``ws.repo_dir`` (the singular
    ``<ws>/repo`` dir) never exists for a meta ticket — so every filesystem
    tool returns "workspace repo directory does not exist" and the agent
    blocks (live: ticket 6e68, a multi-repo PyPI-publication audit). This
    builds the SAME workspace the refine/implement stages use: each required
    repo cloned into ``<ws>/repos/<id>``.

    The investigation root is the **common parent** of the clones
    (``<ws>/repos``), NOT the first clone. ``run_command`` and ``explore``
    sandbox to the root dir only (``sandbox.run(repo_dir=root)`` does not
    mount ``extra_roots``), so rooting at the first clone would let the agent
    ``read_file`` siblings but leave them invisible to ``run_command``/``ls``/
    ``explore`` — the audit then reports "8/9 repos absent" (live: 6e68).
    Rooting at the parent makes every clone a subdir of the one mounted root,
    so all tools traverse all repos via ``<repo-id>/...`` paths.

    Returns ``(meta_root, meta_extra_roots, blocking_result)``. For a
    non-meta ticket returns ``(None, [], None)``. When the meta workspace
    can't be built, ``blocking_result`` is a failing
    :class:`MaintenanceResult` the caller should return directly.
    """
    if ticket.board_id != "meta":
        return None, [], None

    from ..meta.workspace import build_triaged_meta_workspace

    meta_repo_dir, meta_roots, meta_outcome = build_triaged_meta_workspace(
        ctx, ticket, ws, draft, author="maintenance"
    )
    if meta_outcome is not None:
        # Triage error or no repo cloned — surface as a blocking result
        # (build_triaged_meta_workspace already added an explanatory comment).
        return (
            None,
            [],
            MaintenanceResult(
                success=False,
                note=meta_outcome.note or "meta workspace build failed",
            ),
        )
    # Root at the clones' shared parent (``<ws>/repos``) so the sandboxed
    # tools see every clone as a subdir; keep the individual clones as
    # extra_roots too (belt-and-suspenders for path-based tools).
    meta_root = meta_repo_dir.parent if meta_repo_dir is not None else None
    return meta_root, meta_roots or [], None


def run_maintenance_agent(  # noqa: C901 — clone-failure degrade branch + meta/single-repo setup
    ticket: Ticket, ctx: StageContext
) -> MaintenanceResult:
    """Load the maintenance agent definition, build its tool set, run the
    agent loop, and return a structured :class:`MaintenanceResult`.

    Called by :meth:`MaintenanceStage.run`.
    """
    from .base import build_agent_from_definition, _safe_close
    from .retry import run_agent
    from .yaml_loader import load_agent_definition

    # 1. Load the YAML definition
    definition = load_agent_definition(agent_definitions_dir() / "maintenance.yaml")

    # 2. Read the ticket draft (needed for tool + prompt)
    ws = ctx.service.workspace(ticket)
    draft = ws.read_description().strip()

    # 2b. Meta-board tickets are cross-repo — build the multi-repo
    # investigation workspace (see _build_meta_investigation_workspace).
    # ``meta_root`` is the clones' shared parent dir (``<ws>/repos``).
    meta_root, meta_extra_roots, meta_block = _build_meta_investigation_workspace(
        ctx, ticket, ws, draft
    )
    if meta_block is not None:
        return meta_block

    # 3. Create a temporary workspace directory for the clone
    import tempfile

    # 4. Build the tool list (must be inside the tempdir context so
    #    clone_repo can populate the workspace)
    with tempfile.TemporaryDirectory(prefix="maintenance_") as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        # Must mirror make_clone_repo_tool's ``workspace_root / "repo"``
        # clone target so the investigation tools can read the clone.
        clone_dir = tmpdir / "repo"
        # Pre-create the clone target as an empty dir. ``clone_dir`` is
        # wired into the filesystem/explore tools as an extra root before
        # the agent calls ``clone_repo`` to populate it, so an agent that
        # reads/lists a path under it first would otherwise hit an
        # unhandled ``FileNotFoundError`` on the missing directory and
        # Fatal-block the ticket (live case: 81f1). An empty dir lets the
        # tools return graceful "not found" errors instead; ``clone_repo``
        # rmtrees the target before cloning, so this is a no-op for it.
        clone_dir.mkdir(parents=True, exist_ok=True)
        tools: list[Any] = []
        tools.append(make_create_repo_tool(ctx.settings, ctx, draft))
        tools.append(make_fork_repo_tool(ctx.settings))
        tools.append(make_clone_repo_tool(ctx.settings, tmpdir))
        tools.append(make_post_findings_tool(ctx.settings, "maintenance"))

        # Determine the effective root for investigation tools.
        # Priority: an explicit investigation_workspace override, then the
        # meta multi-repo primary clone (for meta-board tickets), then the
        # ticket's own single-repo workspace dir.
        investigation_root = (
            ctx.settings.investigation_workspace
            if ctx.settings.investigation_workspace is not None
            else (meta_root if meta_root is not None else ws.repo_dir)
        )

        # Investigation tools can read the maintenance clone (clone_dir) AND
        # every meta repo clone (meta_extra_roots, empty for non-meta tickets).
        fs_extra_roots = [clone_dir, *meta_extra_roots]

        # Read-only filesystem tools (read_file, list_dir, run_command)
        from .fs_tools import build_fs_tools

        all_fs = build_fs_tools(
            investigation_root, ctx.settings, extra_roots=fs_extra_roots
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

            Only safe, read-only commands are allowed.  Allowed: git, grep,
            ls, find, cat, head, tail, wc, sort, uniq, diff, sed, awk, cut,
            tr, xargs, echo, dirname, basename, realpath, readlink, stat,
            file, du, tree, cd.  Write-capable or destructive commands are
            rejected before execution.
            """
            err = _validate_command(command)
            if err is not None:
                return err
            return _sandbox_run(command)

        tools.append(run_command)

        # Explore / parallel_explore tools scoped to investigation_root
        from .explore import make_explore_tool, make_parallel_explore_tool

        tools.append(
            make_explore_tool(
                ctx.settings, investigation_root, extra_roots=fs_extra_roots
            )
        )
        tools.append(
            make_parallel_explore_tool(
                ctx.settings, investigation_root, extra_roots=fs_extra_roots
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
        # List the registered boards so the agent can name a valid
        # migrate_to_board target when the ticket is misrouted.
        try:
            from ..config import get_repos_config

            board_ids = sorted(
                {rc.board_id for rc in get_repos_config().repos.values()}
            )
            if board_ids:
                repo_context += (
                    "\n# Registered boards (valid migrate_to_board targets)\n"
                    + "\n".join(f"- {b}" for b in board_ids)
                    + "\n"
                )
        except Exception:
            log.debug("could not list registered boards for prompt", exc_info=True)
        if meta_extra_roots:
            # Meta multi-repo ticket: the required repos are ALREADY cloned
            # locally. Point the agent at them so it reads them directly with
            # list_dir/read_file/run_command — do NOT use the single-repo
            # clone_repo tool (it holds only one repo at a time and would
            # overwrite each prior clone).
            repo_context += (
                "\n# Pre-cloned repositories (read these directly)\n"
                "These repos are already cloned locally for this cross-repo "
                "task, each as a SUBDIRECTORY of your working root. Inspect "
                "them with list_dir/read_file/run_command/explore using "
                "``<repo-id>/...`` relative paths (e.g. "
                "``read_file('robotsix-mill/pyproject.toml')`` or "
                "``run_command('cd robotsix-mill && git log -1')``). Do NOT "
                "call clone_repo for them.\n"
                + "\n".join(f"- {p.name}" for p in meta_extra_roots)
                + "\n"
            )
        elif remote_url:
            repo_context += (
                f"\n# Repository clone URL\n{remote_url}\n"
                "(pass this EXACT url to clone_repo — do not guess remotes)\n"
            )
        investigation_guidance = (
            "\n# Investigation guidance\n"
            "- **Prefer direct tools over explore.** For simple file-existence "
            "checks or single-symbol lookups, use ``list_dir`` and ``read_file`` "
            "directly — do NOT delegate them to ``explore``. ``explore`` spawns a "
            "full sub-agent; it is for genuinely complex, multi-step questions that "
            "require navigating several files.\n"
            "- **Narrow explore questions.** When you do use ``explore``, ask a "
            'TIGHT, specific question (e.g. "confirm modules/foo/bar.py exists and '
            'exports class Baz") rather than an open-ended scan (e.g. "find all '
            'files that could cause import errors"). The sub-agent has a small '
            "budget; broad questions exhaust it.\n"
            "- **Use run_command for grep/find.** Pattern searches should use "
            "``run_command(\"grep -rn 'symbol' src/\")`` — never fall back to "
            "reading files one-by-one when a single grep covers the same ground.\n"
            "- **Verify ``::: `` directives locally.** When the task involves "
            "``docs/reference/*.md`` mkdocstrings ``::: module.path`` directives, "
            "read those .md files yourself with ``read_file``, extract the module "
            "paths, then verify each with ``list_dir`` on its parent — do not hand "
            "the whole tree to ``explore``.\n"
            "- **Cloned repos are ephemeral.** The clone created by ``clone_repo`` "
            "is deleted after each LLM turn. If you need to re-access the repo in "
            "a later turn, clone it again. Batch all file reads together in one "
            "turn to avoid repeated clones.\n"
        )
        user_prompt = (
            f"{repo_context}{investigation_guidance}"
            f"\n# Title\n{ticket.title}\n\n# Draft\n{draft}"
        )

        # 7. Run the agent — with an explicit request cap (the implicit
        # pydantic-ai default of 50 is what blocked the data-dir audit
        # tickets with an opaque "Fatal: UsageLimitExceeded").
        from pydantic_ai.usage import UsageLimits

        limits = UsageLimits(request_limit=ctx.settings.maintenance_request_limit)
        try:
            result = run_agent(
                agent,
                lambda h: h.run_sync(user_prompt, usage_limits=limits),
                what="maintenance",
            )
        except (FileNotFoundError, NotADirectoryError) as e:
            # The clone created by clone_repo is ephemeral (deleted after
            # each LLM turn) and a failed clone leaves the target dir
            # absent, so a tool that reads/lists/cd's into the clone path
            # in a later turn raises an unhandled FileNotFoundError that
            # used to Fatal-block the ticket with a raw traceback (live
            # class: cross-repo extraction tickets, /tmp/maintenance_*/repo).
            # Degrade to a clean, actionable block instead of crashing.
            log.warning(
                "maintenance: repository clone path unavailable mid-run", exc_info=True
            )
            return MaintenanceResult(
                success=False,
                note=(
                    "Maintenance investigation could not access the repository "
                    f"clone ({type(e).__name__}: {e}). The clone is ephemeral "
                    "(removed after each turn) and a fresh clone_repo likely "
                    "failed — common when the task targets a repository that is "
                    "not this board's repo (a cross-repo task) or a private repo "
                    "without credentials. If this is cross-repo work, it probably "
                    "belongs on another board (consider migrate_to_board); "
                    "otherwise re-run after confirming the clone URL."
                ),
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
