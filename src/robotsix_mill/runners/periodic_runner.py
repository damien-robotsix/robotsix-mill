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

from ..config import RepoConfig, Settings, get_secrets, target_branch_for
from ..core.models import SourceKind
from ..core.service import TicketService
from ..forge.auth import github_token, gitlab_token
from .pass_runner import run_agent_pass

log = logging.getLogger("robotsix_mill.periodic_runner")


# ---------------------------------------------------------------------------
# Shared result dataclass (replaced 12 structurally-identical copies)
# ---------------------------------------------------------------------------


@dataclass
class PeriodicPassResult:
    """Result of running a periodic pass.

    Replaces the 12 formerly-identical ``*PassResult`` dataclasses.
    """

    updated_memory: str
    drafts_created: list[dict]
    session_id: str = ""


# ---------------------------------------------------------------------------
# Clone-token helpers
# ---------------------------------------------------------------------------


def _clone_token(settings, repo_config) -> str | None:
    """Resolve a clone token for *repo_config*'s forge; return ``None``
    when no credentials are configured (clone will fail and be handled).

    The forge kind is detected from the repo's ``forge_remote_url`` so a
    GitLab-hosted repo gets a GitLab PAT instead of a GitHub App token
    (a GitHub token against ``gitlab.com`` makes ``git clone`` fail with
    an auth error / exit 128).  An ambiguous/custom domain falls back to
    the global ``settings.forge_kind``.
    """
    from ..forge.base import _detect_forge_kind

    kind = settings.forge_kind
    per_repo_url = repo_config.forge_remote_url if repo_config is not None else None
    if per_repo_url:
        try:
            kind = _detect_forge_kind(per_repo_url)
        except RuntimeError:
            kind = settings.forge_kind

    if kind == "gitlab":
        try:
            return gitlab_token()
        except RuntimeError:
            return None

    from ..forge.auth import GitHubAppNotInstalledError

    try:
        return github_token(settings, repo_config=repo_config)
    except GitHubAppNotInstalledError:
        import logging

        _log = logging.getLogger(__name__)
        _log.warning(
            "Clone token unavailable for %s: GitHub App not installed",
            repo_config.repo_id if repo_config else "default",
        )
        return None
    except Exception:
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
    """Short name e.g. ``"audit"``, ``"health"``. Used for logging."""

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

    result_dataclass: type[PeriodicPassResult]
    """The typed result dataclass (e.g. ``PeriodicPassResult``).
    Must have fields ``updated_memory: str``, ``drafts_created: list[dict]``,
    ``session_id: str``."""

    extra_agent_kwargs: dict[str, Any] = field(default_factory=dict)
    """Additional kwargs baked into ``partial(agent_fn, repo_dir=..., **extra_agent_kwargs)``."""

    extra_kwargs_fn: Callable[..., dict[str, Any]] | None = None
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

    requires_repo: bool = False
    """When ``True``, the pass needs a working tree to do useful work.
    If the clone is unavailable (``repo_dir`` resolves to ``None``),
    ``run_periodic_pass`` short-circuits before invoking the LLM — a
    repo-less pass for these agents only produces hallucinated output."""


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
) -> PeriodicPassResult:
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
                target_branch_for(settings, repo_config),
                token_fn(settings, repo_config),
            )
            repo_dir = cand
        except subprocess.CalledProcessError as e:
            log.warning(
                "%s clone failed, web/context-only: %s",
                config.label,
                (e.stderr or "")[:200],
            )

    # Deterministic short-circuit: a pass that needs a working tree but has
    # no clone (forge_remote_url falsy, or git_ops.clone raised) cannot read
    # the repo, so the agent would only fabricate file paths from training
    # data — guaranteed wasted spend. Skip the LLM invocation entirely and
    # return a no-op result. Memory is loaded inside run_agent_pass (skipped
    # here), so no on-disk memory write happens on this path.
    if config.requires_repo and repo_dir is None and forge_remote_url:
        log.warning(
            "%s pass skipped: repo clone unavailable (repo_dir is None); "
            "not invoking LLM",
            config.label,
        )
        return config.result_dataclass(
            updated_memory="",
            drafts_created=[],
            session_id=session_id,
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
        result_dataclass=PeriodicPassResult,
        clone_token_fn=_clone_token,
    ),
    "agent_check": PeriodicPassConfig(
        label="agent_check",
        source_kind=SourceKind.AGENT_CHECK,
        agent_module_attr="agent_check",
        agent_fn_name="run_agent_check_agent",
        memory_filename="agent_check_memory.md",
        workspace_subdir="agent_check_workspace",
        result_dataclass=PeriodicPassResult,
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
        result_dataclass=PeriodicPassResult,
        clone_token_fn=_clone_token,
    ),
    "survey": PeriodicPassConfig(
        label="survey",
        source_kind=SourceKind.SURVEY,
        agent_module_attr="surveying",
        agent_fn_name="run_survey_agent",
        memory_filename="survey_memory.md",
        workspace_subdir="survey_workspace",
        result_dataclass=PeriodicPassResult,
        clone_token_fn=_clone_token,
    ),
    "completeness_check": PeriodicPassConfig(
        label="completeness_check",
        source_kind=SourceKind.COMPLETENESS_CHECK,
        agent_module_attr="completeness_check",
        agent_fn_name="run_completeness_check_agent",
        memory_filename="completeness_check_memory.md",
        workspace_subdir="completeness_check_workspace",
        result_dataclass=PeriodicPassResult,
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
        result_dataclass=PeriodicPassResult,
        clone_token_fn=_clone_token,
    ),
    "forge_parity": PeriodicPassConfig(
        label="forge_parity",
        source_kind=SourceKind.FORGE_PARITY,
        agent_module_attr="forge_parity",
        agent_fn_name="run_forge_parity_agent",
        memory_filename="forge_parity_memory.md",
        workspace_subdir="forge_parity_workspace",
        result_dataclass=PeriodicPassResult,
        clone_token_fn=_clone_token,
    ),
    "config_sync": PeriodicPassConfig(
        label="config_sync",
        source_kind=SourceKind.CONFIG_SYNC,
        agent_module_attr="config_syncing",
        agent_fn_name="run_config_sync_agent",
        memory_filename="config_sync_memory.md",
        workspace_subdir="config_sync_workspace",
        result_dataclass=PeriodicPassResult,
        clone_token_fn=None,  # uses forge_token (raises on missing)
    ),
    "health": PeriodicPassConfig(
        label="health",
        source_kind=SourceKind.HEALTH,
        agent_module_attr="health",
        agent_fn_name="run_health_agent",
        memory_filename="health_memory.md",
        workspace_subdir="health_workspace",
        result_dataclass=PeriodicPassResult,
        clone_token_fn=None,  # uses forge_token (raises on missing)
    ),
    "module_curator": PeriodicPassConfig(
        label="module_curator",
        source_kind=SourceKind.MODULE_CURATOR,
        agent_module_attr="module_curator",
        agent_fn_name="run_module_curator_agent",
        memory_filename="module_curator_memory.md",
        workspace_subdir="module_curator_workspace",
        result_dataclass=PeriodicPassResult,
        clone_token_fn=_clone_token,
        requires_repo=True,
    ),
    "test_gap": PeriodicPassConfig(
        label="test_gap",
        source_kind=SourceKind.TEST_GAP,
        agent_module_attr="test_gap",
        agent_fn_name="run_test_gap_agent",
        memory_filename="test_gap_memory.md",
        workspace_subdir="test_gap_workspace",
        result_dataclass=PeriodicPassResult,
        clone_token_fn=None,  # uses forge_token (raises on missing)
        requires_repo=True,
    ),
    "state_sync": PeriodicPassConfig(
        label="state_sync",
        source_kind=SourceKind.STATE_SYNC,
        agent_module_attr="state_syncing",
        agent_fn_name="run_state_sync_agent",
        memory_filename="state_sync_memory.md",
        workspace_subdir="state_sync_workspace",
        result_dataclass=PeriodicPassResult,
        clone_token_fn=None,  # uses forge_token (raises on missing)
        requires_repo=True,
    ),
    "frontend_sync": PeriodicPassConfig(
        label="frontend_sync",
        source_kind=SourceKind.FRONTEND_SYNC,
        agent_module_attr="frontend_syncing",
        agent_fn_name="run_frontend_sync_agent",
        memory_filename="frontend_sync_memory.md",
        workspace_subdir="frontend_sync_workspace",
        result_dataclass=PeriodicPassResult,
        clone_token_fn=None,  # uses forge_token (raises on missing)
        requires_repo=True,
    ),
    "security_posture": PeriodicPassConfig(
        label="security_posture",
        source_kind=SourceKind.SECURITY_POSTURE,
        agent_module_attr="security_posturing",
        agent_fn_name="run_security_posture_agent",
        memory_filename="security_posture_memory.md",
        workspace_subdir="security_posture_workspace",
        result_dataclass=PeriodicPassResult,
        clone_token_fn=None,  # uses forge_token (raises on missing)
        requires_repo=True,
    ),
    "triage_boilerplate": PeriodicPassConfig(
        label="triage_boilerplate",
        source_kind=SourceKind.TRIAGE_BOILERPLATE,
        agent_module_attr="triage_boilerplate",
        agent_fn_name="run_triage_boilerplate_agent",
        memory_filename="triage_boilerplate_memory.md",
        workspace_subdir="triage_boilerplate_workspace",
        result_dataclass=PeriodicPassResult,
        clone_token_fn=None,  # uses forge_token (raises on missing)
    ),
}


# ---------------------------------------------------------------------------
# Single entry point — replaces 15 structurally-identical stub files
# ---------------------------------------------------------------------------


def run_periodic_pass_entry(
    key: str,
    session_id: str,
    repo_config: RepoConfig | None = None,
) -> PeriodicPassResult:
    """Execute a periodic pass identified by *key* (a ``PERIODIC_PASS_CONFIGS`` key).

    This is the single implementation behind every ``run_*_pass()`` stub.
    The ``Settings`` seam is preserved here so that tests can monkeypatch
    ``robotsix_mill.runners.periodic_runner.Settings``.

    Args:
        key: Config key in ``PERIODIC_PASS_CONFIGS`` (e.g. ``"audit"``).
        session_id: Langfuse session id.
        repo_config: Optional per-repo configuration.

    Returns:
        A ``PeriodicPassResult`` with ``updated_memory``, ``drafts_created``,
        and ``session_id``.
    """
    if key not in PERIODIC_PASS_CONFIGS:
        raise KeyError(
            f"Unknown periodic pass key {key!r}. "
            f"Known: {sorted(PERIODIC_PASS_CONFIGS.keys())}"
        )

    settings = Settings()

    # Survey pass resets web-fetch / web-search budgets before running.
    if key == "survey":
        from ..agents.web_tools import reset_trace_web_fetch_budget
        from ..agents.web_knowledge import reset_trace_web_search_budget

        reset_trace_web_fetch_budget(
            settings.survey_web_fetch_max_calls,
            settings.survey_web_fetch_max_total_bytes,
        )
        reset_trace_web_search_budget(settings.survey_web_search_max_calls)

    return run_periodic_pass(
        session_id,
        repo_config,
        config=PERIODIC_PASS_CONFIGS[key],
        settings=settings,
    )


# ---------------------------------------------------------------------------
# Factory-generated entry points — one module-level name per pass key so
# that CLI `_RUNNERS`, `_make_background_pass`, and test imports all resolve
# to a real callable via `getattr(periodic_runner, "run_audit_pass")`.
# ---------------------------------------------------------------------------


def _make_entry(key: str) -> Callable[[str, RepoConfig | None], PeriodicPassResult]:
    """Return a callable ``(session_id, repo_config=None) -> PeriodicPassResult``
    with a descriptive ``__name__`` for each periodic pass key."""

    def _entry(
        session_id: str,
        repo_config: RepoConfig | None = None,
    ) -> PeriodicPassResult:
        return run_periodic_pass_entry(key, session_id, repo_config)

    _entry.__name__ = key  # e.g. "audit", "health", …
    _entry.__qualname__ = f"run_{key}_pass"
    return _entry


run_audit_pass = _make_entry("audit")
run_agent_check_pass = _make_entry("agent_check")
run_bc_check_pass = _make_entry("bc_check")
run_survey_pass = _make_entry("survey")
run_completeness_check_pass = _make_entry("completeness_check")
run_copy_paste_pass = _make_entry("copy_paste")
run_forge_parity_pass = _make_entry("forge_parity")
run_config_sync_pass = _make_entry("config_sync")
run_health_pass = _make_entry("health")
run_module_curator_pass = _make_entry("module_curator")
run_test_gap_pass = _make_entry("test_gap")
run_state_sync_pass = _make_entry("state_sync")
run_frontend_sync_pass = _make_entry("frontend_sync")
run_security_posture_pass = _make_entry("security_posture")
run_triage_boilerplate_pass = _make_entry("triage_boilerplate")
