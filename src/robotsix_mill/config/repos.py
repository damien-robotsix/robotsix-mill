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

from typing import Any

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
    langfuse_public_key: str
    langfuse_secret_key: str
    langfuse_base_url: str = "https://cloud.langfuse.com"
    # Optional reference to another repo_id whose Langfuse project this
    # repo inherits. When set, this repo MUST NOT supply its own langfuse
    # keys; load_repos_config resolves the reference by copying the
    # referenced (master) repo's langfuse_project_name/public_key/
    # secret_key/base_url into this entry, so the whole workspace shares
    # one Langfuse project. Populated by the workspace member-sync
    # mechanism. None -> this repo uses its own langfuse block.
    langfuse_from: str | None = None
    # Per-repo OpenRouter inference key. When set, cost-reconciliation runs in
    # PER-KEY mode for this repo (snapshot this key's cumulative usage each pass
    # + diff against the prior snapshot) so its provider spend reconciles
    # against ITS Langfuse project. Unset → account-level activity reconcile.
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


def _validate_no_partial_langfuse(
    repos: dict[str, RepoConfig], raw_langfuse: dict[str, dict[str, Any]]
) -> None:
    """Reject a partially-specified Langfuse block.

    The public/secret key pair is the canonical "observability is
    configured" signal (mirrors the meta-config logic in
    :func:`load_repos_config`, which builds a project only when BOTH keys
    are present). Both present → configured; both absent/empty → no
    observability (unchanged behavior). Exactly one present is a
    half-configured block that would fail opaquely at runtime — raise
    :class:`~robotsix_mill.config.loader.ConfigError`. Repos that inherit
    via ``langfuse_from`` carry no own keys and are validated separately,
    so they are skipped here.
    """
    from .loader import ConfigError

    for repo_id, cfg in repos.items():
        if cfg.langfuse_from is not None:
            continue
        own_langfuse = raw_langfuse.get(repo_id, {})
        has_public = bool(own_langfuse.get("public_key"))
        has_secret = bool(own_langfuse.get("secret_key"))
        if has_public and not has_secret:
            raise ConfigError(
                f"Repo '{repo_id}' supplies langfuse.public_key but is missing "
                f"langfuse.secret_key; both are required to enable observability "
                f"(or omit the langfuse block entirely)."
            )
        if has_secret and not has_public:
            raise ConfigError(
                f"Repo '{repo_id}' supplies langfuse.secret_key but is missing "
                f"langfuse.public_key; both are required to enable observability "
                f"(or omit the langfuse block entirely)."
            )


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
    """Load repos configuration from ``config/repos.yaml`` (or override).

    Reads YAML via :func:`~robotsix_mill.config.loader.load_repos_yaml`,
    constructs a :class:`RepoConfig` for each entry, validates, and
    returns a :class:`ReposRegistry`.
    """
    from .loader import load_meta_yaml, load_repos_yaml

    raw = load_repos_yaml(config_file)
    repos: dict[str, RepoConfig] = {}
    raw_langfuse: dict[str, dict] = {}
    for repo_id, repo_data in raw.items():
        langfuse = repo_data.get("langfuse", {}) if isinstance(repo_data, dict) else {}
        raw_langfuse[repo_id] = langfuse if isinstance(langfuse, dict) else {}
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
        repos[repo_id] = RepoConfig(
            repo_id=repo_id,
            board_id=repo_data.get("board_id", "")
            if isinstance(repo_data, dict)
            else "",
            langfuse_project_name=langfuse.get("project_name", ""),
            langfuse_public_key=langfuse.get("public_key", ""),
            langfuse_secret_key=langfuse.get("secret_key", ""),
            langfuse_base_url=langfuse.get("base_url", "https://cloud.langfuse.com"),
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
            langfuse_from=repo_data.get("langfuse_from")
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
        )

    # Resolve ``langfuse_from`` references: a member repo inherits the
    # referenced master's Langfuse project, so the whole workspace shares one
    # project. Enforce the operator rule that a referencing repo must NOT
    # carry its own keys, and reject unknown / chained / self references.
    from .loader import ConfigError

    for repo_id, cfg in list(repos.items()):
        if cfg.langfuse_from is None:
            continue
        own_langfuse = raw_langfuse.get(repo_id, {})
        if (
            own_langfuse.get("project_name")
            or own_langfuse.get("public_key")
            or own_langfuse.get("secret_key")
        ):
            raise ConfigError(
                f"Repo '{repo_id}' sets langfuse_from='{cfg.langfuse_from}' but "
                f"also supplies its own langfuse keys; a repo inheriting a "
                f"Langfuse project must not carry separate keys."
            )
        if cfg.langfuse_from not in repos:
            known = sorted(repos.keys())
            raise ConfigError(
                f"Repo '{repo_id}' references unknown langfuse_from "
                f"'{cfg.langfuse_from}'. Known repos: {known}"
            )
        master = repos[cfg.langfuse_from]
        if master.langfuse_from is not None:
            raise ConfigError(
                f"Repo '{repo_id}' references langfuse_from "
                f"'{cfg.langfuse_from}', which itself sets langfuse_from "
                f"'{master.langfuse_from}'; langfuse_from must point at a "
                f"master repo that holds its own keys (no chaining or "
                f"self-reference)."
            )
        repos[repo_id] = cfg.model_copy(
            update={
                "langfuse_project_name": master.langfuse_project_name,
                "langfuse_public_key": master.langfuse_public_key,
                "langfuse_secret_key": master.langfuse_secret_key,
                "langfuse_base_url": master.langfuse_base_url,
            }
        )

    # Reject a partially-specified Langfuse block (extracted to keep this
    # function's cyclomatic complexity in check).
    _validate_no_partial_langfuse(repos, raw_langfuse)

    # Reject a cross_repo_target on any repo when the global forge kind is
    # GitLab (the GitLab adapter has no cross-fork MR support).
    from .settings import load_settings

    _validate_cross_repo_forge_compat(repos, load_settings().forge_kind)

    # Optional dedicated Langfuse project for the synthetic cross-repo
    # meta board. Built only when a ``meta:`` block supplies usable
    # credentials (public + secret key); otherwise the meta-agent traces
    # nowhere, exactly as before this block existed.
    meta_raw = load_meta_yaml(config_file)
    meta_langfuse = meta_raw.get("langfuse", {}) if isinstance(meta_raw, dict) else {}
    meta_config: RepoConfig | None = None
    if meta_langfuse.get("public_key") and meta_langfuse.get("secret_key"):
        meta_config = RepoConfig(
            repo_id="meta",
            board_id="meta",
            langfuse_project_name=meta_langfuse.get("project_name", "meta"),
            langfuse_public_key=meta_langfuse["public_key"],
            langfuse_secret_key=meta_langfuse["secret_key"],
            langfuse_base_url=meta_langfuse.get(
                "base_url", "https://cloud.langfuse.com"
            ),
        )

    # Single-project consolidation: when ``langfuse_shared_master`` is set
    # at the top level, force EVERY repo (and the meta board) onto that
    # master repo's Langfuse project, overriding any per-repo ``langfuse``
    # block. One switch collapses the whole workspace into one project
    # (sessions stay legible via the repo-qualified session id — see
    # runtime.tracing.qualify_session).
    meta_config = _apply_langfuse_shared_master(repos, meta_config, config_file)

    return ReposRegistry(repos=repos, meta=meta_config)


def _apply_langfuse_shared_master(
    repos: dict[str, RepoConfig],
    meta_config: RepoConfig | None,
    config_file: str | None,
) -> RepoConfig | None:
    """Force all repos + meta onto ``langfuse_shared_master``'s project.

    No-op (returns *meta_config* unchanged) when the switch is unset.
    Raises :class:`ConfigError` when the named master is unknown or
    carries no Langfuse keys to share.
    """
    from .loader import ConfigError, load_langfuse_shared_master

    shared_master = load_langfuse_shared_master(config_file)
    if shared_master is None:
        return meta_config
    if shared_master not in repos:
        raise ConfigError(
            f"langfuse_shared_master='{shared_master}' is not a known repo. "
            f"Known repos: {sorted(repos.keys())}"
        )
    master = repos[shared_master]
    if not (master.langfuse_public_key and master.langfuse_secret_key):
        raise ConfigError(
            f"langfuse_shared_master='{shared_master}' must itself carry "
            f"Langfuse keys (public_key + secret_key) to share."
        )
    lf_fields = {
        "langfuse_project_name": master.langfuse_project_name,
        "langfuse_public_key": master.langfuse_public_key,
        "langfuse_secret_key": master.langfuse_secret_key,
        "langfuse_base_url": master.langfuse_base_url,
    }
    for repo_id, cfg in list(repos.items()):
        if repo_id != shared_master:
            repos[repo_id] = cfg.model_copy(update=lf_fields)
    if meta_config is not None:
        return meta_config.model_copy(update=lf_fields)
    return RepoConfig(
        repo_id="meta",
        board_id="meta",
        langfuse_project_name=master.langfuse_project_name,
        langfuse_public_key=master.langfuse_public_key,
        langfuse_secret_key=master.langfuse_secret_key,
        langfuse_base_url=master.langfuse_base_url,
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


def _reset_repos_config() -> None:
    """Clear the cached :class:`ReposRegistry` singleton (for tests)."""
    import robotsix_mill.config as _pkg

    _pkg._repos_config = None
