"""The :class:`Settings` model and ``load_settings()``.

Assembles the field mixins (``_settings_core``, ``_settings_stages``,
``_settings_periodic``, ``_settings_observability``) with
``BaseSettings`` and carries ``model_config``, the
``settings_customise_sources`` hook, the path/property helpers, and the
cross-field validators. Split out of the former monolithic ``config.py``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from ._settings_core import _CoreSettings
from ._settings_observability import _ObservabilitySettings
from ._settings_periodic import _PeriodicSettings
from ._settings_stages import _StagesSettings
from .secrets import get_secrets
from .yaml_source import YamlSettingsSource

log = logging.getLogger(__name__)


class Settings(
    # Mixin order is reversed relative to the original field declaration
    # order because pydantic collects fields in reverse-MRO order; listing
    # the mixins back-to-front here preserves the original
    # ``Settings.model_fields`` ordering (core → stages → periodic →
    # observability).
    _ObservabilitySettings,
    _PeriodicSettings,
    _StagesSettings,
    _CoreSettings,
    BaseSettings,
):
    """Central Pydantic configuration model for robotsix-mill.

    All fields are sourced from ``os.environ`` and layered
    ``config/*.yaml`` files.  Conventional keys like
    ``OPENROUTER_API_KEY`` or ``LANGFUSE_*`` are unprefixed to remain
    compatible with the reference projects.  Mill-specific settings use
    the ``MILL_`` / ``FORGE_`` prefix convention and declare explicit
    ``Field(alias=...)`` values.
    """

    model_config = SettingsConfigDict(
        # ``extra="forbid"``: an unknown kwarg is a typo or a stale
        # MILL_*-style legacy alias from a feature branch written
        # before the YAML-only refactor. Silent drops let those
        # branches "pass" locally and explode in CI after rebase —
        # exactly the failure mode that BLOCKED ticket ad2f's PR.
        # Forbidding the unknown kwarg surfaces the typo at the call
        # site, where the implement agent can see and fix it.
        #
        # ``env_prefix="MILL_"``: fields without an explicit
        # ``Field(alias=...)`` derive their env-var name as
        # ``MILL_<field_name>`` (e.g. ``model`` → ``MILL_MODEL``).
        # Fields WITH an explicit alias (e.g. ``FORGE_KIND``,
        # ``OPENROUTER_API_KEY``) use that alias verbatim — the
        # prefix is NOT applied.
        env_prefix="MILL_",
        env_file_encoding="utf-8",
        extra="forbid",
        populate_by_name=True,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Insert YAML source with second-lowest priority (above only
        Field defaults), so ``os.environ`` still overrides it.

        Precedence (highest to lowest):
        1. explicit ``Settings(k=v)`` kwargs
        2. ``os.environ``
        3. file secrets
        4. ``config/*.yaml`` layered YAML
        5. Field(default=…) static defaults
        """
        return (
            init_settings,
            env_settings,
            file_secret_settings,
            YamlSettingsSource(settings_cls),
        )

    def workspaces_dir_for(self, board_id: str) -> Path:
        """Per-repo workspaces directory. *board_id* is required —
        raises ``ValueError`` when empty."""
        if not board_id:
            raise ValueError(
                "workspaces_dir_for: board_id is required. "
                "The board-less <data_dir>/workspaces is gone."
            )
        return self.data_dir / board_id / "workspaces"

    def memory_file_for(self, name: str, board_id: str) -> Path:
        """Return the per-repo memory ledger path for *name*
        (e.g. ``"implement"``, ``"refine"``, ``"audit"``).

        Honors any explicit ``<name>_memory_path`` setting override
        (env / YAML); otherwise routes to
        ``<data_dir>/<board_id>/<name>_memory.md``.  *board_id* is
        required — raises ``ValueError`` when empty.

        Memory ledgers are repo-specific observation logs (codebase
        conventions, testing patterns, gotchas) — each repo
        accumulates its own.
        """
        if not board_id:
            raise ValueError(
                "memory_file_for: board_id is required. "
                "The board-less <data_dir>/<name>_memory.md is gone."
            )
        override = getattr(self, f"{name}_memory_path", None)
        if override is not None:
            return override
        return self.data_dir / board_id / f"{name}_memory.md"

    @property
    def tracing_enabled(self) -> bool:
        """True when all three Langfuse credentials are configured."""
        secrets = get_secrets()
        return bool(
            secrets.langfuse_base_url
            and secrets.langfuse_public_key
            and secrets.langfuse_secret_key
        )

    @property
    def retrospect_memory_file(self) -> Path:
        """Resolved path to the agent-maintained retrospect memory ledger."""
        if self.retrospect_memory_path is not None:
            return self.retrospect_memory_path
        return self.data_dir / "retrospect_memory.md"

    @property
    def trace_inspector_memory_file(self) -> Path:
        """Resolved path to the trace inspector's memory ledger."""
        if self.trace_inspector_memory_path is not None:
            return self.trace_inspector_memory_path
        return self.data_dir / "trace_inspector_memory.md"

    @property
    def audit_memory_file(self) -> Path:
        """Resolved path to the agent-maintained audit memory ledger."""
        if self.audit_memory_path is not None:
            return self.audit_memory_path
        return self.data_dir / "audit_memory.md"

    @property
    def agent_check_memory_file(self) -> Path:
        """Resolved path to the agent-maintained agent-check memory ledger."""
        if self.agent_check_memory_path is not None:
            return self.agent_check_memory_path
        return self.data_dir / "agent_check_memory.md"

    @property
    def health_memory_file(self) -> Path:
        """Resolved path to the agent-maintained health memory ledger."""
        if self.health_memory_path is not None:
            return self.health_memory_path
        return self.data_dir / "health_memory.md"

    @property
    def test_gap_memory_file(self) -> Path:
        """Resolved path to the test-gap agent's Markdown memory ledger."""
        if self.test_gap_memory_path is not None:
            return self.test_gap_memory_path
        return self.data_dir / "test_gap_memory.md"

    @property
    def survey_memory_file(self) -> Path:
        """Resolved path to the agent-maintained survey memory ledger."""
        if self.survey_memory_path is not None:
            return self.survey_memory_path
        return self.data_dir / "survey_memory.md"

    @property
    def config_sync_memory_file(self) -> Path:
        """Resolved path to the agent-maintained config-sync memory ledger."""
        if self.config_sync_memory_path is not None:
            return self.config_sync_memory_path
        return self.data_dir / "config_sync_memory.md"

    @property
    def state_sync_memory_file(self) -> Path:
        """Resolved path to the state-sync agent's Markdown memory ledger."""
        if self.state_sync_memory_path is not None:
            return self.state_sync_memory_path
        return self.data_dir / "state_sync_memory.md"

    @property
    def env_doc_sync_memory_file(self) -> Path:
        """Resolved path to the env-doc-sync agent's Markdown memory ledger."""
        if self.env_doc_sync_memory_path is not None:
            return self.env_doc_sync_memory_path
        return self.data_dir / "env_doc_sync_memory.md"

    @property
    def bc_check_memory_file(self) -> Path:
        """Resolved path to the agent-maintained bc-check memory ledger."""
        if self.bc_check_memory_path is not None:
            return self.bc_check_memory_path
        return self.data_dir / "bc_check_memory.md"

    @property
    def completeness_check_memory_file(self) -> Path:
        """Resolved path to the agent-maintained completeness-check memory ledger."""
        if self.completeness_check_memory_path is not None:
            return self.completeness_check_memory_path
        return self.data_dir / "completeness_check_memory.md"

    @property
    def implement_memory_file(self) -> Path:
        """Resolved path to the agent-maintained implement memory ledger."""
        if self.implement_memory_path is not None:
            return self.implement_memory_path
        return self.data_dir / "implement_memory.md"

    @property
    def refine_memory_file(self) -> Path:
        """Resolved path to the agent-maintained refine memory ledger."""
        if self.refine_memory_path is not None:
            return self.refine_memory_path
        return self.data_dir / "refine_memory.md"

    @property
    def doc_memory_file(self) -> Path:
        """Resolved path to the agent-maintained document memory ledger."""
        if self.doc_memory_path is not None:
            return self.doc_memory_path
        return self.data_dir / "doc_memory.md"

    @property
    def ci_fix_memory_file(self) -> Path:
        """Resolved path to the agent-maintained ci-fix memory ledger."""
        if self.ci_fix_memory_path is not None:
            return self.ci_fix_memory_path
        return self.data_dir / "ci_fix_memory.md"

    @property
    def rebase_memory_file(self) -> Path:
        """Resolved path to the agent-maintained rebase memory ledger."""
        if self.rebase_memory_path is not None:
            return self.rebase_memory_path
        return self.data_dir / "rebase_memory.md"

    @property
    def ci_patterns_file(self) -> Path:
        """Resolved path to the ci-fix agent's structured pattern memory."""
        if self.ci_patterns_path is not None:
            return self.ci_patterns_path
        return self.data_dir / "ci_patterns.json"

    def ci_patterns_file_for(self, board_id: str = "") -> Path:
        """Per-repo resolved path for the ci-fix pattern memory.

        Falls back to the global path when no board_id is provided or
        when ``ci_patterns_path`` is explicitly overridden in config.
        """
        if self.ci_patterns_path is not None:
            return self.ci_patterns_path
        if board_id:
            return self.data_dir / board_id / "ci_patterns.json"
        return self.data_dir / "ci_patterns.json"

    # ------------------------------------------------------------------
    #  Validators
    # ------------------------------------------------------------------

    # -- interval minimums ---------------------------------------------

    @field_validator("trace_health_interval_seconds")
    @classmethod
    def _validate_trace_health_interval(cls, v: int) -> int:
        if v < 3600:
            raise ValueError("trace_health_interval_seconds must be ≥ 3600")
        return v

    @field_validator("trace_review_interval_seconds")
    @classmethod
    def _validate_trace_review_interval(cls, v: int) -> int:
        if v < 3600:
            raise ValueError("trace_review_interval_seconds must be ≥ 3600")
        return v

    # -- cross-field checks --------------------------------------------

    @model_validator(mode="after")
    def _validate_cross_field(self) -> "Settings":
        # forge_auth=app is GitHub-only — reject for GitLab early so
        # the error message is specific, not a misleading GitHub App
        # credential complaint.
        if self.forge_auth == "app" and self.forge_kind == "gitlab":
            raise ValueError(
                "FORGE_AUTH=app is not supported with FORGE_KIND=gitlab; "
                "use FORGE_AUTH=token and set FORGE_TOKEN to a GitLab PAT"
            )

        # forge_auth=app requires GitHub App credentials
        if self.forge_auth == "app":
            if not self.github_app_id and not self.github_app_private_key_path:
                raise ValueError(
                    "FORGE_AUTH=app requires at least one of github_app_id "
                    "or github_app_private_key_path to be set"
                )

        # forge_kind needs forge_remote_url (auto-detection also needs a URL)
        if self.forge_kind in ("github", "gitlab", "auto"):
            if not self.forge_remote_url:
                raise ValueError(
                    f"forge_kind={self.forge_kind} requires forge_remote_url to be set"
                )

        return self


def load_settings() -> Settings:
    """Load and return a :class:`Settings` instance from env / ``.env`` files."""
    return Settings()
