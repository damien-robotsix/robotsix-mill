"""Runtime configuration, sourced from a single JSON config file
(``config/config.json`` or ``ROBOTSIX_CONFIG_FILE``).

Conventional keys (``OPENROUTER_API_KEY``, ``LANGFUSE_*``) are
unprefixed to match the reference projects; mill-specific knobs use the
``MILL_`` / ``FORGE_`` prefixes.

This package was split out of a single 2000+ line ``config.py`` module
by responsibility, while preserving the exact public API via the
re-exports below — every ``from robotsix_mill.config import ...`` site
keeps working unchanged. Submodules:

- ``mill_config`` — :class:`MillConfig` top-level model loaded via
  ``robotsix_config.load_config`` and cached.
- ``settings`` — the :class:`Settings` model (assembled from the
  ``_settings_*`` field mixins) and :func:`load_settings`.
- ``secrets`` — the :class:`Secrets` model (with :class:`pydantic.SecretStr`
  fields) and its cached accessors.
- ``repos`` — the per-repo :class:`CrossRepoTarget` / :class:`RepoConfig`
  / :class:`ReposRegistry` models and their loaders.

The cached config singleton lives in ``mill_config.py`` so test
fixtures that assign ``robotsix_mill.config._repos_config`` are
still observed by the submodule accessors.
"""

from __future__ import annotations

from .loader import ConfigError
from .mill_config import MillConfig, _reset_config_cache
from .repos import (
    CrossRepoTarget,
    RepoConfig,
    ReposRegistry,
    _reset_repos_config,
    effective_target_branch,
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

# Cached repos config singleton — kept here so test fixtures poking
# ``robotsix_mill.config._repos_config`` are visible to the accessors
# in ``repos.py``.
_repos_config: ReposRegistry | None = None

# Backward-compat: test fixtures assign ``robotsix_mill.config._secrets``
# to inject a mock Secrets instance.  When set, :func:`get_secrets`
# returns it instead of loading from the JSON file.
_secrets: Secrets | None = None

__all__ = [
    "ConfigError",
    "MillConfig",
    "_reset_config_cache",
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
    "effective_target_branch",
    "_reset_repos_config",
]
