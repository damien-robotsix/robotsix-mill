"""Centralized periodic-pass runner factory.

Defines ``PeriodicPassConfig`` and ``run_periodic_pass`` — the single
shared implementation behind all 8 ``*_runner.py`` stubs.  Each stub
delegates to this module with a config key, keeping the existing
import paths and monkeypatch seams intact.

Adding a new periodic agent is now a one-line registry entry instead
of a ~120-line file copy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Callable

from ..config import RepoConfig, get_secrets
from ..core.models import SourceKind
from ..core.service import TicketService
from ..forge.auth import github_token
from .pass_runner import run_agent_pass

log = logging.getLogger("robotsix_mill.periodic_runner")


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class AuditPassResult:
    """Result of running an audit pass."""

    updated_memory: str
    drafts_created: list[dict]
    session_id: str = ""
    proposed_actions: list[dict] = field(default_factory=list)


@dataclass
class AgentCheckPassResult:
    """Result of running an agent-check pass."""

    updated_memory: str
    drafts_created: list[dict]
    session_id: str = ""
    proposed_actions: list[dict] = field(default_factory=list)


@dataclass
class BcCheckPassResult:
    """Result of running a bc-check pass."""

    updated_memory: str
    drafts_created: list[dict]
    session_id: str = ""
    proposed_actions: list[dict] = field(default_factory=list)


@dataclass
class SurveyPassResult:
    """Result of running a survey pass."""

    updated_memory: str
    drafts_created: list[dict]
    session_id: str = ""
    proposed_actions: list[dict] = field(default_factory=list)


@dataclass
class CompletenessCheckPassResult:
    """Result of running a completeness-check pass."""

    updated_memory: str
    drafts_created: list[dict]
    session_id: str = ""
    proposed_actions: list[dict] = field(default_factory=list)


@dataclass
class CopyPastePassResult:
    """Result of running a copy-paste pass."""

    updated_memory: str
    drafts_created: list[dict]
    session_id: str = ""
    proposed_actions: list[dict] = field(default_factory=list)


@dataclass
class ConfigSyncPassResult:
    """Result of running a config-sync pass."""

    updated_memory: str
    drafts_created: list[dict]
    session_id: str = ""
    proposed_actions: list[dict] = field(default_factory=list)


@dataclass
class HealthPassResult:
    """Result of running a health pass."""

    updated_memory: str
    drafts_created: list[dict]
    session_id: str = ""
    proposed_actions: list[dict] = field(default_factory=list)


@dataclass
class ModuleCuratorPassResult:
    """Result of running a module-curator pass."""

    updated_memory: str
    drafts_created: list[dict]
    session_id: str = ""
    proposed_actions: list[dict] = field(default_factory=list)


@dataclass
class TestGapPassResult:
    """Result of running a test-gap pass."""

    updated_memory: str
    drafts_created: list[dict]
    session_id: str = ""
    proposed_actions: list[dict] = field(default_factory=list)


TestGapPassResult.__test__ = False


@dataclass
class BoardCleanupPassResult:
    """Result of running a board-cleanup pass."""

    updated_memory: str
    drafts_created: list[dict]
    session_id: str = ""
    proposed_actions: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Clone-token helpers
# ---------------------------------------------------------------------------


def _clone_token(settings, repo_config) -> str | None:
    """Resolve a clone token via ``github_token``; return ``None`` when
    no credentials are configured (clone will fail and be handled)."""
    try:
        return github_token(settings, repo_config=repo_config)
    except RuntimeError:
        return None


def _forge_token(settings, repo_config) -> str | None:
    """Resolve the forge token via ``get_secrets().forge_token``.
    Raises if the secret is missing — used by health, test_gap,
    config_sync, and completeness_check runners."""
    return get_secrets().forge_token


# ---------------------------------------------------------------------------
# Configuration dataclass
# ---------------------------------------------------------------------------


@dataclass
class PeriodicPassConfig:
    """Configuration for a single periodic pass.

    Describes how to import the agent module, resolve paths, pick a
    clone-token strategy, and wrap the result in the correct dataclass.
    """

    label: str
    """Short name e.g. ``"audit"``, ``"health"``.
    Drives ``settings.memory_file_for(label, ...)`` and logging."""

    source_kind: SourceKind
    """``SourceKind`` enum value passed to ``run_agent_pass(source_label=...)``."""

    agent_module_attr: str
    """Agent module attribute name for lazy import.
    E.g. ``"auditing"`` → ``from .agents import auditing``."""

    agent_fn_name: str
    """Function name on the agent module, e.g. ``"run_audit_agent"``."""

    memory_filename: str
    """Per-repo memory file stem, e.g. ``"audit_memory.md"``."""

    workspace_subdir: str
    """Clone workspace subdirectory, e.g. ``"audit_workspace"``."""

    result_dataclass: type
    """The typed result dataclass (e.g. ``AuditPassResult``).
    Must have fields ``updated_memory: str``, ``drafts_created: list[dict]``,
    ``session_id: str``."""

    extra_agent_kwargs: dict = field(default_factory=dict)
    """Additional kwargs baked into ``partial(agent_fn, repo_dir=..., **extra_agent_kwargs)``."""

    extra_kwargs_fn: Callable[..., dict] | None = None
    """Optional callable ``(settings) -> dict`` whose returned keys are
    merged into ``extra_agent_kwargs`` at runtime.  Used by agent_check
    to inject ``memory_dir=settings.data_dir``."""

    max_drafts: int | None = None
    """Passed as ``max_drafts=`` to ``run_agent_pass``.  ``None`` means default."""

    max_drafts_fn: Callable[..., int] | None = None
    """Optional callable ``(settings) -> int`` that returns the
    ``max_drafts`` value at runtime.  Used by completeness_check
    to read ``MAX_GAPS`` from the agent module."""

    clone_token_fn: Callable[..., str | None] | None = None
    """Resolves forge clone token.
    ``None`` → use ``get_secrets().forge_token`` (raise on missing).
    A callable ``(settings, repo_config) -> str | None`` wraps
    ``github_token()`` with fallback."""


# ---------------------------------------------------------------------------
# Core runner function
# ---------------------------------------------------------------------------


def run_periodic_pass(
    session_id: str,
    repo_config: RepoConfig | None,
    config: PeriodicPassConfig,
    *,
    settings,
    definition_override: Any = None,
) -> Any:
    """Execute one full periodic pass.

    This is the deduplicated body of the 8 ``run_*_pass()`` functions.
    The caller (the stub module) is responsible for creating and
    injecting ``Settings`` so the existing monkeypatch seam
    ``robotsix_mill.<name>_runner.Settings`` continues to work.

    Args:
        session_id: Langfuse session id from the poll loop.
        repo_config: Optional per-repo configuration for multi-repo
            serve. When provided, ticket creation and memory files
            are scoped to this repo.
        config: Descriptor for this periodic pass.
        settings: Pre-resolved ``Settings`` instance injected by the
            stub.

    Returns:
        An instance of ``config.result_dataclass`` with
        ``updated_memory``, ``drafts_created``, and ``session_id``.
    """

    clone_dir: Path | None = None
    forge_remote_url = settings.forge_remote_url

    if repo_config is None:
        # Periodic passes must run against a registered repo. The
        # legacy board-less fallback that wrote to <data_dir>/mill.db
        # is gone; tests should pass an explicit repo_config.
        raise ValueError(
            f"run_periodic_pass({config.label}): repo_config is "
            "required — configure at least one repo in "
            "config/repos.yaml and pass its RepoConfig in."
        )
    service = TicketService(settings, board_id=repo_config.board_id)
    repo_data_dir = settings.data_dir / repo_config.repo_id
    repo_data_dir.mkdir(parents=True, exist_ok=True)
    memory_file = repo_data_dir / config.memory_filename
    if repo_config.forge_remote_url:
        forge_remote_url = repo_config.forge_remote_url
        clone_dir = repo_data_dir / config.workspace_subdir / "repo"

    # Lazy import the agent module (keeps monkeypatch seam alive).
    import importlib

    agent_module = importlib.import_module(
        f".agents.{config.agent_module_attr}", package="robotsix_mill"
    )
    from ..vcs import git_ops

    # Resolve clone token strategy.
    token_fn = (
        config.clone_token_fn if config.clone_token_fn is not None else _forge_token
    )

    # Clone the repo locally so the agent inspects it via
    # explore/read_file instead of web-fetching the project's own
    # files. Idempotent; best-effort.
    repo_dir = None
    if forge_remote_url:
        import shutil
        import subprocess

        cand = clone_dir or (settings.data_dir / config.workspace_subdir / "repo")
        # Each periodic run starts from a CLEAN, fresh clone. Wipe any prior
        # workspace first: a reused clone keeps whatever commit it was last
        # left at, so the agent would analyse a STALE tree (and miss work
        # already merged upstream). Fresh clone = always current origin tip.
        if cand.exists():
            shutil.rmtree(cand, ignore_errors=True)
        try:
            git_ops.clone(
                forge_remote_url,
                cand,
                settings.forge_target_branch,
                token_fn(settings, repo_config),
            )
            repo_dir = cand
        except subprocess.CalledProcessError as e:
            log.warning(
                "%s clone failed, web/context-only: %s",
                config.label,
                (e.stderr or "")[:200],
            )

    log.info("%s pass starting (session %s)", config.label, session_id)

    # Resolve runtime kwargs.
    extra_kwargs = dict(config.extra_agent_kwargs)
    if config.extra_kwargs_fn is not None:
        extra_kwargs.update(config.extra_kwargs_fn(settings))
    max_drafts = config.max_drafts
    if config.max_drafts_fn is not None:
        max_drafts = config.max_drafts_fn(settings)

    agent_fn_callable = getattr(agent_module, config.agent_fn_name)
    # Thread the per-repo merged definition (resolved by the periodic
    # supervisor from .robotsix-mill/periodic/<name>.yaml) into the agent fn
    # when present; agent fns ignore it when None (legacy built-in path).
    if definition_override is not None:
        extra_kwargs["definition_override"] = definition_override
    agent_fn = partial(agent_fn_callable, repo_dir=repo_dir, **extra_kwargs)
    result = run_agent_pass(
        agent_fn=agent_fn,
        memory_file=memory_file,
        source_label=config.source_kind,
        service=service,
        settings=settings,
        origin_session=session_id,
        max_drafts=max_drafts,
        repo_dir=repo_dir,
    )

    return config.result_dataclass(
        updated_memory=result.updated_memory,
        drafts_created=result.drafts_created,
        session_id=session_id,
        proposed_actions=result.proposed_actions,
    )


# ---------------------------------------------------------------------------
# Registry — map label → config
# ---------------------------------------------------------------------------


def _completeness_max_gaps() -> int:
    """Return ``MAX_GAPS`` from the completeness_check agent module."""
    from ..agents import completeness_check

    return completeness_check.MAX_GAPS


PERIODIC_PASS_CONFIGS: dict[str, PeriodicPassConfig] = {
    "audit": PeriodicPassConfig(
        label="audit",
        source_kind=SourceKind.AUDIT,
        agent_module_attr="auditing",
        agent_fn_name="run_audit_agent",
        memory_filename="audit_memory.md",
        workspace_subdir="audit_workspace",
        result_dataclass=AuditPassResult,
        clone_token_fn=_clone_token,
    ),
    "agent_check": PeriodicPassConfig(
        label="agent_check",
        source_kind=SourceKind.AGENT_CHECK,
        agent_module_attr="agent_check",
        agent_fn_name="run_agent_check_agent",
        memory_filename="agent_check_memory.md",
        workspace_subdir="agent_check_workspace",
        result_dataclass=AgentCheckPassResult,
        clone_token_fn=_clone_token,
        extra_kwargs_fn=lambda settings: {"memory_dir": settings.data_dir},
    ),
    "bc_check": PeriodicPassConfig(
        label="bc_check",
        source_kind=SourceKind.BC_CHECK,
        agent_module_attr="bc_check",
        agent_fn_name="run_bc_check_agent",
        memory_filename="bc_check_memory.md",
        workspace_subdir="bc_check_workspace",
        result_dataclass=BcCheckPassResult,
        clone_token_fn=_clone_token,
    ),
    "survey": PeriodicPassConfig(
        label="survey",
        source_kind=SourceKind.SURVEY,
        agent_module_attr="surveying",
        agent_fn_name="run_survey_agent",
        memory_filename="survey_memory.md",
        workspace_subdir="survey_workspace",
        result_dataclass=SurveyPassResult,
        clone_token_fn=_clone_token,
    ),
    "completeness_check": PeriodicPassConfig(
        label="completeness_check",
        source_kind=SourceKind.COMPLETENESS_CHECK,
        agent_module_attr="completeness_check",
        agent_fn_name="run_completeness_check_agent",
        memory_filename="completeness_check_memory.md",
        workspace_subdir="completeness_check_workspace",
        result_dataclass=CompletenessCheckPassResult,
        clone_token_fn=None,  # uses forge_token (raises on missing)
        max_drafts_fn=lambda _settings: _completeness_max_gaps(),
    ),
    "copy_paste": PeriodicPassConfig(
        label="copy_paste",
        source_kind=SourceKind.COPY_PASTE,
        agent_module_attr="copy_pasting",
        agent_fn_name="run_copy_paste_agent",
        memory_filename="copy_paste_memory.md",
        workspace_subdir="copy_paste_workspace",
        result_dataclass=CopyPastePassResult,
        clone_token_fn=_clone_token,
    ),
    "config_sync": PeriodicPassConfig(
        label="config_sync",
        source_kind=SourceKind.CONFIG_SYNC,
        agent_module_attr="config_syncing",
        agent_fn_name="run_config_sync_agent",
        memory_filename="config_sync_memory.md",
        workspace_subdir="config_sync_workspace",
        result_dataclass=ConfigSyncPassResult,
        clone_token_fn=None,  # uses forge_token (raises on missing)
    ),
    "health": PeriodicPassConfig(
        label="health",
        source_kind=SourceKind.HEALTH,
        agent_module_attr="health",
        agent_fn_name="run_health_agent",
        memory_filename="health_memory.md",
        workspace_subdir="health_workspace",
        result_dataclass=HealthPassResult,
        clone_token_fn=None,  # uses forge_token (raises on missing)
    ),
    "module_curator": PeriodicPassConfig(
        label="module_curator",
        source_kind=SourceKind.MODULE_CURATOR,
        agent_module_attr="module_curator",
        agent_fn_name="run_module_curator_agent",
        memory_filename="module_curator_memory.md",
        workspace_subdir="module_curator_workspace",
        result_dataclass=ModuleCuratorPassResult,
        clone_token_fn=_clone_token,
    ),
    "test_gap": PeriodicPassConfig(
        label="test_gap",
        source_kind=SourceKind.TEST_GAP,
        agent_module_attr="test_gap",
        agent_fn_name="run_test_gap_agent",
        memory_filename="test_gap_memory.md",
        workspace_subdir="test_gap_workspace",
        result_dataclass=TestGapPassResult,
        clone_token_fn=None,  # uses forge_token (raises on missing)
    ),
}


# ---------------------------------------------------------------------------
# Bespoke board-cleanup pass
# ---------------------------------------------------------------------------
#
# board_cleanup does not fit the generic PeriodicPassConfig shape: unlike the
# code-oriented periodic agents it operates on the BOARD (existing tickets),
# not the code tree, so it needs the full board snapshot injected into its
# prompt. recent_proposals_for() (what run_agent_pass injects) only returns the
# agent's OWN prior proposals — insufficient to spot stale tickets from other
# sources. So a small bespoke runner fetches the board via recent_tickets() and
# threads it into the agent, then delegates the proposed-action / draft /
# memory persistence to the shared run_agent_pass.


def _render_board_snapshot(tickets) -> str:
    """Render a compact one-line-per-ticket snapshot of the board for
    agent-prompt injection: ``[STATE] short_id | title``, most recent
    first."""
    if not tickets:
        return "(no tickets on the board)"
    lines = []
    for t in tickets:
        short_id = t.id[:7]
        state_val = t.state.value
        lines.append(f"[{state_val}] {short_id} | {t.title}")
    return "\n".join(lines)


def run_board_cleanup_pass(
    session_id: str,
    repo_config: RepoConfig,
    *,
    settings,
    definition_override: Any = None,
) -> BoardCleanupPassResult:
    """Execute one board-cleanup pass.

    Fetches a snapshot of recent board tickets (across all sources),
    threads it into ``run_board_cleanup_agent``, and delegates proposed
    action / draft / memory persistence to the shared
    :func:`run_agent_pass`.

    Args:
        session_id: Langfuse session id from the poll loop.
        repo_config: Per-repo configuration. Required — the legacy
            board-less fallback is gone (cf. ``run_periodic_pass``).
        settings: Pre-resolved ``Settings`` instance injected by the
            caller so the monkeypatch seam stays intact.
        definition_override: The per-repo merged agent definition
            resolved by the periodic supervisor, threaded into the
            agent fn (ignored when ``None``).

    Returns:
        A ``BoardCleanupPassResult``.
    """
    if repo_config is None:
        raise ValueError(
            "run_board_cleanup_pass: repo_config is required — configure "
            "at least one repo in config/repos.yaml and pass its "
            "RepoConfig in."
        )

    service = TicketService(settings, board_id=repo_config.board_id)
    memory_file = settings.board_cleanup_memory_file(repo_config.repo_id)
    memory_file.parent.mkdir(parents=True, exist_ok=True)

    # Fetch the full board (across all sources) so the agent can spot
    # stale/obsolete tickets, not just its own prior proposals.
    try:
        board = service.recent_tickets(limit=200)
    except Exception:
        log.debug(
            "run_board_cleanup_pass: recent_tickets() failed — "
            "passing an empty board snapshot (DB may not be initialised)"
        )
        board = []
    board_snapshot = _render_board_snapshot(board)

    from ..agents import board_cleanup as agent_module

    log.info("board_cleanup pass starting (session %s)", session_id)

    agent_fn = partial(
        agent_module.run_board_cleanup_agent,
        repo_dir=None,
        board_snapshot=board_snapshot,
        definition_override=definition_override,
    )
    result = run_agent_pass(
        agent_fn=agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.BOARD_CLEANUP,
        service=service,
        settings=settings,
        origin_session=session_id,
        repo_dir=None,
    )

    return BoardCleanupPassResult(
        updated_memory=result.updated_memory,
        drafts_created=result.drafts_created,
        session_id=session_id,
        proposed_actions=result.proposed_actions,
    )
