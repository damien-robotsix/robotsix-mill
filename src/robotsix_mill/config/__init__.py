"""Runtime configuration, sourced from environment, .env, and secrets.env.

Conventional keys (``OPENROUTER_API_KEY``, ``LANGFUSE_*``) are
unprefixed to match the reference projects; mill-specific knobs use the
``MILL_`` / ``FORGE_`` prefixes.

This package was split out of a single 2000+ line ``config.py`` module
by responsibility, while preserving the exact public API via the
re-exports below — every ``from robotsix_mill.config import ...`` site
keeps working unchanged. Submodules:

- ``json_source`` — :class:`JsonSettingsSource`, the pydantic-settings
  JSON source.
- ``settings`` — the :class:`Settings` model (assembled from the
  ``_settings_*`` field mixins) and :func:`load_settings`.
- ``secrets`` — the :class:`Secrets` model and its cached accessors.
- ``repos`` — the per-repo :class:`CrossRepoTarget` / :class:`RepoConfig`
  / :class:`ReposRegistry` models and their loaders.

The cached ``_secrets`` / ``_repos_config`` singletons are defined here
(not in the submodules) so test fixtures that assign
``robotsix_mill.config._secrets`` / ``._repos_config`` are observed by
the submodule accessors, which read these package attributes at call
time.
"""

from __future__ import annotations

from .loader import ConfigError
from .repos import (
    CrossRepoTarget,
    RepoConfig,
    ReposRegistry,
    _reset_repos_config,
    get_repo_config,
    get_repos_config,
    load_repos_config,
    target_branch_for,
)
from .secrets import (
    Secrets,
    _reset_secrets,
    get_secrets,
    load_secrets,
    logger,
)
from .settings import Settings, load_settings
from .json_source import JsonSettingsSource

# Cached singletons live here so test fixtures poking
# ``robotsix_mill.config._secrets`` / ``._repos_config`` are visible to
# the accessors in ``secrets.py`` / ``repos.py``.
_secrets: Secrets | None = None
_repos_config: ReposRegistry | None = None

__all__ = [
    "ConfigError",
    "JsonSettingsSource",
    "Settings",
    "load_settings",
    "Secrets",
    "load_secrets",
    "get_secrets",
    "_reset_secrets",
    "logger",
    "CrossRepoTarget",
    "RepoConfig",
    "ReposRegistry",
    "load_repos_config",
    "get_repos_config",
    "get_repo_config",
    "target_branch_for",
    "_reset_repos_config",
]
