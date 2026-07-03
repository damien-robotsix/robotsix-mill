"""Per-repo configuration models and loaders.

``CrossRepoTarget`` / ``RepoConfig`` / ``ReposRegistry`` plus the
``load_repos_config`` / ``get_repos_config`` / ``get_repo_config`` /
``target_branch_for`` helpers, split out of the former monolithic
``config.py``. The cached ``_repos_config`` singleton lives in
``config/__init__.py`` so test fixtures that poke
``robotsix_mill.config._repos_config`` are observed by the accessors
here (which read the package attribute at call time).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, field_validator, model_validator

from .settings import Settings


class CrossRepoTarget(BaseModel):
    """Declarative *cross-repo target* for a repo whose deliverable
    belongs in a different (forked/external) repository.

    When a :class:`RepoConfig` carries one of these, the deliver/merge
    stages drive a fork-contribution workflow: the ticket's branch is
    pushed to ``fork_remote_url`` and a PR is opened *fork → upstream*
    against ``base_branch`` on ``upstream_remote_url`` (the merge
    target Y), instead of pushing to the clone remote.

    Fields:
    - ``upstream_remote_url`` — the repo PRs are opened against (Y).
    - ``fork_remote_url`` — the fork the branch is pushed to.
    - ``base_branch`` — the upstream branch to PR into (mirrors
      ``forge_target_branch``).
    - ``auto_fork`` — when True, ensure the fork exists via
      ``Forge.fork_repo()`` before push.
    """

    upstream_remote_url: str
    fork_remote_url: str
    base_branch: str = "main"
    auto_fork: bool = False

    @field_validator("upstream_remote_url", "fork_remote_url", "base_branch")
    @classmethod
    def _validate_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be non-empty")
        return v


class RepoConfig(BaseModel):
    """Configuration for a single repository — its board identity,
    Langfuse observability project credentials, and per-repo CI
    monitor settings."""

    repo_id: str
    board_id: str
    langfuse_project_name: str
    langfuse_project_id: str = ""
    langfuse_public_key: str
    langfuse_secret_key: str
    langfuse_base_url: str = "https://cloud.langfuse.com"
    # NOTE: the langfuse_* fields above are populated centrally from the
    # global Langfuse credentials in the config.yaml secrets block (see _apply_global_langfuse).
    # They are identical across every repo — there is no per-repo Langfuse
    # configuration.
    # Per-repo OpenRouter inference key.
    openrouter_api_key: str | None = None
    forge_remote_url: str | None = None
    # Optional path to the live deployment's log directory for this repo.
    # This is a deployment-specific host path (canonically absolute), so it
    # lives here in the operator's central, gitignored ``config/repos.yaml``
    # — NOT the managed repo's committed ``.robotsix-mill/config.yaml`` — to
    # avoid leaking deployment layout into the repo. When set and pointing at
    # an existing directory, the refine agent gets read access to it
    # (extra_roots + log-query tool). ``None``/empty → no log access.
    deployed_log_folder: str | None = None
    # Optional pinned working branch. When set, member repos branch from
    # and open PRs into this branch (e.g. "lyrical") instead of the fork's
    # default branch. Populated from the vcs2l manifest `version` by the
    # workspace member-sync mechanism; None → ordinary default-branch behaviour.
    working_branch: str | None = None
    # Optional per-repo sandbox image override. When set, this repo's
    # sandbox executions (test/smoke gates + the implement coordinator's
    # interactive run_command) use this image; ``None`` → fall back to
    # ``settings.sandbox_image``. Deliberately operator-controlled here in
    # ``config/repos.yaml`` (NOT the repo's own ``.robotsix-mill/config.yaml``
    # where test_command/extra_sandbox_packages live): selecting the base
    # Docker image is a higher-trust knob than declaring packages on a
    # trusted base. ``.robotsix-mill/config.yaml`` is committed in the managed
    # repo and editable by any PR; letting a PR pick an arbitrary base image
    # whose on-PATH binaries run with the repo bind-mounted is a sandbox-trust
    # escalation. Keeping it in operator-controlled ``config/repos.yaml`` keeps
    # image selection on the trusted side of the boundary.
    sandbox_image: str | None = None
    # Optional cross-repo target: when set, deliver pushes the ticket
    # branch to ``fork_remote_url`` and opens a fork→upstream PR against
    # ``upstream_remote_url``/``base_branch`` instead of the clone
    # remote. ``None`` → ordinary same-repo delivery (unchanged).
    cross_repo_target: CrossRepoTarget | None = None
    ci_monitor_enabled: bool = True
    # Default 900s (15 min): main-branch CI breaks should be turned into
    # tickets within minutes, not a day. A repo may override via the
    # ``ci_monitor.interval_seconds`` field in repos.yaml. Min 60 enforced.
    ci_monitor_interval_seconds: int = 900
    # Number of tickets from THIS repo the worker will process in
    # parallel. Per-repo isolation: each repo gets its own consumer
    # pool, so a busy repo can't starve another. Default 1 keeps the
    # blast radius of any one ticket's bad behaviour contained.
    max_concurrency: int = 1
    # Max number of in-flight PR tickets (DELIVERABLE through ADDRESSING_REVIEW)
    # before the worker stops dispatching new READY/DRAFT work for this repo.
    # Merge-pipeline tickets are always processed.  HUMAN_MR_APPROVAL, BLOCKED,
    # and AWAITING_USER_REPLY do NOT count.  Set to 0 to disable (current behavior).
    max_inflight_prs: int = 3
    # Source discriminator: ``"config"`` for operator-configured entries,
    # ``"auto"`` for machine-registered overlay entries.
    source: Literal["config", "auto"] = "config"
    # NOTE: per-repo ``test_command`` and ``language`` were REMOVED from
    # repos.yaml. A managed repo now owns both in its own source tree via
    # ``.robotsix-mill/config.yaml`` (``test_command`` + ``languages``); the
    # mill reads them from the clone (repo_settings.py). The global
    # ``Settings.test_command`` remains as the fleet-wide test-gate fallback.
    #
    # NOTE: the per-repo ``*_periodic`` enable flags were also REMOVED. A
    # periodic workflow now runs for a repo iff the repo ships
    # ``.robotsix-mill/periodic/<name>.yaml`` (file presence = enabled; see
    # agents/periodic_loader.py + the worker's periodic supervisor). The
    # global ``Settings.<name>_periodic`` switches remain as fleet-wide
    # kill-switches.

    @field_validator("repo_id", "board_id")
    @classmethod
    def _validate_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be non-empty")
        return v

    @field_validator("ci_monitor_interval_seconds")
    @classmethod
    def _validate_ci_monitor_interval_seconds(cls, v: int) -> int:
        if v < 60:
            raise ValueError("ci_monitor_interval_seconds must be ≥ 60")
        return v

    @field_validator("max_concurrency")
    @classmethod
    def _validate_max_concurrency(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_concurrency must be ≥ 1")
        return v

    @field_validator("max_inflight_prs")
    @classmethod
    def _validate_max_inflight_prs(cls, v: int) -> int:
        if v < 0:
            raise ValueError("max_inflight_prs must be ≥ 0")
        return v


class ReposRegistry(BaseModel):
    """Container holding all :class:`RepoConfig` entries keyed by repo ID."""

    repos: dict[str, RepoConfig]
    # Optional Langfuse config for the synthetic cross-repo *meta* board.
    # The meta-agent is not a registered repo (no clone/forge), so it is
    # kept OUT of ``repos`` — but it gets its own dedicated Langfuse
    # project here so its passes trace just like the per-repo pipelines.
    # ``None`` when no ``meta:`` block is configured → meta runs untraced.
    meta: RepoConfig | None = None

    @model_validator(mode="after")
    def _validate_keys_match_repo_ids(self) -> "ReposRegistry":
        for key, config in self.repos.items():
            if config.repo_id != key:
                raise ValueError(
                    f"Repo key '{key}' does not match "
                    f"RepoConfig.repo_id '{config.repo_id}'"
                )
        return self


def _validate_cross_repo_forge_compat(
    repos: dict[str, RepoConfig], forge_kind: str
) -> None:
    """Reject a ``cross_repo_target`` on a repo when the global forge kind
    is GitLab.

    The GitLab forge adapter does not support cross-fork merge requests
    (``cross_repo_target`` is GitHub-only); without this check the
    misconfiguration only surfaces at runtime in the deliver stage as a
    ``NotImplementedError``. Only the explicit ``"gitlab"`` value is
    rejected — ``"auto"`` resolves the forge kind from the remote URL
    later and cannot be statically known here, so rejecting it would
    produce false positives.
    """
    from .loader import ConfigError

    if forge_kind != "gitlab":
        return
    for repo_id, cfg in repos.items():
        if cfg.cross_repo_target is not None:
            raise ConfigError(
                f"Repo '{repo_id}' sets cross_repo_target, but the GitLab forge "
                f"adapter does not support cross-fork merge requests "
                f"(cross_repo_target is GitHub-only). Remove cross_repo_target or "
                f"set FORGE_KIND to github."
            )


def load_repos_config(config_file: str | None = None) -> ReposRegistry:
    """Load repos configuration from ``config/config.json``'s ``repos:``
    key (or the ``MILL_REPOS_FILE`` / *config_file* override).

    Reads YAML via :func:`~robotsix_mill.config.loader.load_repos_yaml`,
    constructs a :class:`RepoConfig` for each entry, validates, and
    returns a :class:`ReposRegistry`.
    """
    from .loader import load_repos_yaml

    raw = load_repos_yaml(config_file)
    repos: dict[str, RepoConfig] = {}
    for repo_id, repo_data in raw.items():
        ci_monitor = (
            repo_data.get("ci_monitor", {}) if isinstance(repo_data, dict) else {}
        )
        cross_repo_raw = (
            repo_data.get("cross_repo_target") if isinstance(repo_data, dict) else None
        )
        cross_repo_target = (
            CrossRepoTarget(**cross_repo_raw)
            if isinstance(cross_repo_raw, dict)
            else None
        )
        source_tag = (
            repo_data.get("_mill_source", "config")
            if isinstance(repo_data, dict)
            else "config"
        )
        # Langfuse is configured GLOBALLY (top-level ``langfuse`` block —
        # see _apply_global_langfuse), never per repo. Each repo starts
        # with empty langfuse fields and is populated from the global block.
        repos[repo_id] = RepoConfig(
            repo_id=repo_id,
            board_id=repo_data.get("board_id", "")
            if isinstance(repo_data, dict)
            else "",
            langfuse_project_name="",
            langfuse_project_id="",
            langfuse_public_key="",
            langfuse_secret_key="",
            openrouter_api_key=repo_data.get("openrouter_api_key")
            if isinstance(repo_data, dict)
            else None,
            forge_remote_url=repo_data.get("forge_remote_url")
            if isinstance(repo_data, dict)
            else None,
            deployed_log_folder=repo_data.get("deployed_log_folder")
            if isinstance(repo_data, dict)
            else None,
            working_branch=repo_data.get("working_branch")
            if isinstance(repo_data, dict)
            else None,
            sandbox_image=repo_data.get("sandbox_image")
            if isinstance(repo_data, dict)
            else None,
            cross_repo_target=cross_repo_target,
            ci_monitor_enabled=ci_monitor.get("enabled", True)
            if isinstance(ci_monitor, dict)
            else True,
            ci_monitor_interval_seconds=ci_monitor.get("interval_seconds", 900)
            if isinstance(ci_monitor, dict)
            else 900,
            max_concurrency=repo_data.get("max_concurrency", 1)
            if isinstance(repo_data, dict)
            else 1,
            max_inflight_prs=repo_data.get("max_inflight_prs", 3)
            if isinstance(repo_data, dict)
            else 3,
            source="auto" if source_tag == "auto" else "config",
        )

    # Reject a cross_repo_target on any repo when the global forge kind is
    # GitLab (the GitLab adapter has no cross-fork MR support).
    from .settings import load_settings

    _validate_cross_repo_forge_compat(repos, load_settings().forge_kind)

    # Single global Langfuse project: the langfuse_* keys in the config.yaml secrets block
    # configure observability for EVERY repo and the meta board. There is no
    # per-repo Langfuse config (sessions stay per-repo legible via the
    # repo-qualified session id — see runtime.tracing.qualify_session).
    meta_config = _apply_global_langfuse(repos)

    return ReposRegistry(repos=repos, meta=meta_config)


def _apply_global_langfuse(repos: dict[str, RepoConfig]) -> "RepoConfig | None":
    """Populate every repo and the meta board from the global Langfuse
    credentials in the config.yaml ``secrets:`` block (``Secrets.langfuse_*``) — the one place
    Langfuse is configured.

    Returns the meta-board ``RepoConfig`` (or ``None`` when the credentials
    are absent / incomplete, i.e. observability is off). There is no per-repo
    Langfuse configuration.
    """
    from .secrets import get_secrets

    s = get_secrets()
    pk, sk = s.langfuse_public_key, s.langfuse_secret_key
    if not (pk and sk):
        return None
    project_name = s.langfuse_project_name or s.langfuse_project_id or ""
    project_id = s.langfuse_project_id or ""
    base_url = s.langfuse_base_url or "https://cloud.langfuse.com"
    lf_fields = {
        "langfuse_project_name": project_name,
        "langfuse_project_id": project_id,
        "langfuse_public_key": pk,
        "langfuse_secret_key": sk,
        "langfuse_base_url": base_url,
    }
    for repo_id, cfg in list(repos.items()):
        repos[repo_id] = cfg.model_copy(update=lf_fields)
    return RepoConfig(
        repo_id="meta",
        board_id="meta",
        langfuse_project_name=project_name,
        langfuse_project_id=project_id,
        langfuse_public_key=pk,
        langfuse_secret_key=sk,
        langfuse_base_url=base_url,
    )


def get_repos_config() -> ReposRegistry:
    """Return a cached :class:`ReposRegistry` singleton, constructing it
    on first call."""
    import robotsix_mill.config as _pkg

    cached = _pkg._repos_config
    if cached is None:
        cached = load_repos_config()
        _pkg._repos_config = cached
    return cached


def get_repo_config(repo_id: str) -> RepoConfig:
    """Look up *repo_id* in :func:`get_repos_config` and return its
    :class:`RepoConfig`.

    Raises :class:`~robotsix_mill.config.loader.ConfigError` for unknown IDs.
    """
    from .loader import ConfigError

    registry = get_repos_config()
    try:
        return registry.repos[repo_id]
    except KeyError as err:
        sorted_keys = sorted(registry.repos.keys())
        raise ConfigError(
            f"Unknown repo: '{repo_id}'. Known repos: {sorted_keys}"
        ) from err


def target_branch_for(settings: Settings, repo_config: RepoConfig | None) -> str:
    """Effective target branch: repo_config.working_branch when set,
    else settings.forge_target_branch (zero change for existing boards)."""
    if repo_config is not None and repo_config.working_branch:
        return repo_config.working_branch
    return settings.forge_target_branch


def effective_target_branch(settings: Settings, repo_config: RepoConfig | None) -> str:
    """Resolve the effective target branch for git operations.

    When *repo_config* has a ``cross_repo_target``, use its
    ``base_branch`` (e.g. ``develop`` on the upstream fork target).
    Otherwise fall back to :func:`target_branch_for`.
    """
    if repo_config is not None and repo_config.cross_repo_target is not None:
        return repo_config.cross_repo_target.base_branch
    return target_branch_for(settings, repo_config)


def _reset_repos_config() -> None:
    """Clear the cached :class:`ReposRegistry` singleton (for tests)."""
    import robotsix_mill.config as _pkg

    _pkg._repos_config = None


def resolve_child_board_id(
    repo_id: str,
    epic_board_id: str,
    epic_id: str,
    repos: "ReposRegistry | None" = None,
) -> str:
    """Resolve a child's ``repo_id`` to a ``board_id`` for child creation.

    On unknown or empty *repo_id*, falls back to *epic_board_id* and
    emits a ``log.warning`` naming the epic, the bad repo_id, and the
    fallback board.  NEVER raises — every child must be created
    somewhere, even when the agent emits an unrecognised repo.

    *repos* is the :class:`ReposRegistry`; when ``None`` it is loaded
    via :func:`get_repos_config`.
    """
    import logging

    log = logging.getLogger("robotsix_mill.config")

    if repos is None:
        try:
            repos = get_repos_config()
        except Exception:
            log.warning(
                "epic %s: cannot load repos config for child repo_id %r — "
                "falling back to epic board %r",
                epic_id,
                repo_id,
                epic_board_id,
            )
            return epic_board_id

    if not repo_id or not repo_id.strip():
        return epic_board_id

    if repo_id not in repos.repos:
        log.warning(
            "epic %s: unknown child repo_id %r — falling back to epic board %r",
            epic_id,
            repo_id,
            epic_board_id,
        )
        return epic_board_id

    return repos.repos[repo_id].board_id
