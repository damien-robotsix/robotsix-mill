"""Single-entry-point config model for the robotsix-mill JSON config file.

``MillConfig`` mirrors the top-level structure of ``config/config.json``
(and the committed ``config/config.example.json`` template)::

    {
      "settings": { ... },
      "secrets":  { ... },
      "repos":    { ... }
    }

It is loaded via :func:`robotsix_config.load_config` (the fleet-standard
JSON config loader) and cached so the rest of the application reads from
a single validated instance.  The legacy hand-rolled ``load_config()``
and ``JsonSettingsSource`` are removed — the JSON file is the one source
of config values.

Secrets fields use :class:`pydantic.SecretStr`; the ``robotsix-config``
loader writes cleartext into the ``0600`` file and the schema marks them
as ``writeOnly`` password fields for the deploy UI.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field
from robotsix_config import load_config as _rc_load_config

from .secrets import Secrets
from .settings import Settings


class MillConfig(BaseModel):
    """Top-level config model — one file, one source of truth.

    The JSON file's top-level keys map directly to these fields.
    ``repos`` is kept as a raw dict because the repos pipeline
    (operator entries + ``registered_repos.yaml`` overlay) is
    post-processed by :func:`~.loader.load_repos_config`.
    """

    settings: Settings = Field(
        default_factory=Settings,
        description="All non-secret runtime settings. Keys are flat field aliases (e.g. MILL_MAX_GLOBAL_CONCURRENCY).",
    )
    secrets: Secrets = Field(
        default_factory=Secrets,
        description="API keys, tokens, and credentials. Fields use SecretStr so the deploy UI renders password inputs.",
    )
    repos: dict[str, object] = Field(
        default_factory=dict,
        description="Operator-configured repo registry (merged with the machine-owned overlay at load time).",
    )


# ---------------------------------------------------------------------------
# Cached singleton + loader
# ---------------------------------------------------------------------------

_config_cache: MillConfig | None = None


def load_mill_config(
    config_file: str | Path | None = None,
) -> MillConfig:
    """Load (or return the cached) :class:`MillConfig` from the JSON file.

    On first call reads ``ROBOTSIX_CONFIG_FILE`` (or ``config/config.json``)
    via :func:`robotsix_config.load_config` and caches the validated
    instance.  Subsequent calls return the cached instance immediately.

    When ``config/config.json`` is missing (e.g. in CI), falls back to
    the committed ``config/config.example.json`` template so the test
    suite and hermetic runs get sensible defaults.

    Pass an explicit *config_file* to override the path (used by tests);
    when given the cache is bypassed so a test that monkeypatches the
    file sees a fresh load.
    """
    global _config_cache
    if config_file is not None:
        return _rc_load_config(MillConfig, path=Path(config_file))

    if _config_cache is not None:
        return _config_cache

    from robotsix_config.config import DEFAULT_CONFIG_PATH, resolve_config_path

    target = resolve_config_path()
    if not target.exists():
        target = DEFAULT_CONFIG_PATH.parent / "config.example.json"
    _config_cache = _rc_load_config(MillConfig, path=target)
    return _config_cache


def _reset_config_cache() -> None:
    """Clear the cached config singleton (for tests)."""
    global _config_cache
    _config_cache = None
