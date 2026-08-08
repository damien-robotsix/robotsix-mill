"""The :class:`Settings` model and ``load_settings()``.

Settings fields are assembled from four mixins (core, stages,
periodic, observability) and wired into ``BaseSettings`` with
JSON-file sourcing, env-var aliases, and cross-field validators.
All fields are sourced from ``os.environ`` and a single JSON config
file (``config/config.json`` or the committed
``config/config.example.json`` template).  Conventional keys like
``LANGFUSE_*`` are unprefixed to remain compatible with the reference
projects.  Secret credentials (``OPENROUTER_API_KEY``, ``FORGE_TOKEN``,
etc.) live in :class:`~robotsix_mill.config.Secrets`, not in
``Settings``.  Mill-specific settings use
the ``MILL_`` / ``FORGE_`` prefix convention and declare explicit
``Field(alias=...)`` values.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import robotsix_config
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from ._settings_core import _CoreSettings
from ._settings_observability import _ObservabilitySettings
from ._settings_periodic import _PeriodicSettings
from ._settings_stages import _StagesSettings
from .json_source import JsonSettingsSource

if TYPE_CHECKING:
    from .repos import ReposRegistry

log = logging.getLogger(__name__)


class LangfuseProjectCredentials(BaseModel):
    """Credentials for a single Langfuse project.

    The canonical block shape per robotsix-standards#189.
    """

    public_key: str
    secret_key: str
    project_id: str


class LangfuseConfig(BaseModel):
    """Canonical Langfuse configuration block (robotsix-standards#189).

    Holds the Langfuse host and exactly one project entry for the
    component's own LLM function.  Per-repo credentials remain on
    ``RepoConfig`` and are NOT registered here.
    """

    host: str = "https://langfuse.robotsix.net"
    projects: dict[str, LangfuseProjectCredentials]


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
    """Runtime settings for robotsix-mill: concurrency limits,
    API endpoints, feature toggles, stage-level controls,
    sandbox configuration, and agent budgets.
    """

    # --- Secrets (folded into Settings as SecretStr fields) ---
    forge_repo_create_token: SecretStr | None = Field(
        default=None,
        description="A PAT used ONLY for repository creation (POST /user/repos). Falls back to forge_token if unset. GitHub App tokens cannot create repos — use a classic PAT with repo-creation scope.",
    )
    sandbox_push_token: SecretStr | None = Field(
        default=None,
        description="Optional dedicated token for the sandbox git-push bridge. When set, github_push_token() prefers this over forge_token (PAT mode only; App mode always mints a fresh token). Use this to isolate the push-bridge credential surface from the general forge token — a broken push token then only blocks pushes, not PR creation or API calls.",
    )
    # --- Langfuse (canonical block, robotsix-standards#189) ---
    langfuse: LangfuseConfig | None = Field(
        default=None,
        description="Langfuse configuration for mill's own LLM tracing (host + one project entry). Per-repo credentials live on RepoConfig and are NOT registered here.",
    )
    openrouter_management_key: SecretStr | None = Field(
        default=None,
        description="OpenRouter management API key for credit-balance polling (https://openrouter.ai/keys).",
    )
    ntfy_url: SecretStr | None = Field(
        default=None,
        description="ntfy server URL for push notifications (https://ntfy.sh).",
    )
    ntfy_token: SecretStr | None = Field(
        default=None,
        description="ntfy access token for authenticated push notifications.",
    )

    # --- Repository registry ---
    repos: ReposRegistry | None = Field(
        default=None,
        description="Repository registry.",
    )

    model_config = SettingsConfigDict(
        extra="forbid",
        populate_by_name=True,
        # Disable .env file loading — env vars only.
        env_file=None,
        # All non-aliased fields get MILL_ prefix (e.g. MILL_BC_CHECK_PERIODIC).
        # Fields with explicit aliases (FORGE_AUTH, OPENROUTER_API_KEY, etc.)
        # use their alias instead, unaffected by this prefix.
        env_prefix="MILL_",
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
        """Read the JSON config file at second-lowest priority — above only
        the Field defaults, so ``os.environ`` still wins.

        Precedence, highest first:

        1. explicit ``Settings(k=v)`` kwargs
        2. ``os.environ``
        3. file secrets
        4. the JSON config file
        5. ``Field(default=...)``

        Without this the model has no file source at all: the cutover (#2525)
        moved file loading into ``load_settings()``, which nothing calls, so
        ``Settings()`` returned defaults and the operator's config was
        ignored everywhere.
        """
        return (
            init_settings,
            env_settings,
            file_secret_settings,
            JsonSettingsSource(settings_cls),
        )

    def workspaces_dir_for(self, board_id: str) -> Path:
        """Per-repo workspaces directory. *board_id* is required —
        raises ``ValueError`` when empty.
        """
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
        """True when the Langfuse config block is present and has at least one project."""
        return self.langfuse is not None and bool(self.langfuse.projects)

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

    def diagnostic_events_file_for(self, board_id: str = "") -> Path:
        """Per-repo resolved path for the diagnostic event store.

        Honors ``diagnostic_events_path`` override (env / YAML);
        otherwise routes to ``<data_dir>/<board_id>/diagnostic_events.jsonl``.
        """
        if self.diagnostic_events_path is not None:
            return self.diagnostic_events_path
        if board_id:
            return self.data_dir / board_id / "diagnostic_events.jsonl"
        return self.data_dir / "diagnostic_events.jsonl"

    # ------------------------------------------------------------------
    #  Validators
    # ------------------------------------------------------------------

    @model_validator(mode="before")
    @classmethod
    def _strip_removed_langfuse_secrets(cls, data: Any) -> Any:
        """Strip removed ``secrets.langfuse_*`` keys from incoming config.

        When a deployed config.json still carries the old flat
        ``secrets.langfuse_*`` fields after the image upgrade, this
        validator drops them so ``extra="forbid"`` does not reject the
        config and crash-loop the process.  The values are discarded
        (never read) — an unmigrated deployment starts, traces nothing,
        and reports no projects to central-deploy, which is visible and
        fixable without an outage.
        """
        if not isinstance(data, dict):
            return data
        removed = [
            "langfuse_base_url",
            "langfuse_project_id",
            "langfuse_project_name",
            "langfuse_public_key",
            "langfuse_secret_key",
        ]
        stripped = {k: v for k, v in data.items() if k not in removed}
        if len(stripped) != len(data):
            log.warning(
                "Dropped removed secrets.langfuse_* keys from config: %s. "
                "The config must be migrated to the canonical langfuse block "
                "(robotsix-standards#189).",
                sorted(set(data) & set(removed)),
            )
        return stripped

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

    # -- stage budgets --------------------------------------------------

    @property
    def ci_fix_agent_budget_seconds(self) -> int:
        """Wall-clock the ci-fix agent is *configured* to be allowed.

        The ci-fix agent owns the fix→push→verify loop: it may call
        ``wait_for_ci`` up to ``ci_fix_max_iterations`` times, and each
        call blocks for up to ``ci_fix_wait_timeout_s``. Add one
        coordinator budget for the LLM/edit work between waits and that
        is the most the stage can legitimately take.
        """
        waits = self.ci_fix_max_iterations * self.ci_fix_wait_timeout_s
        return int(waits + self.coordinator_timeout_seconds)

    def stage_timeout_for(self, stage_name: str) -> int:
        """Resolve the wall-clock timeout for *stage_name*.

        Normally the explicit override, else ``stage_timeout_seconds``.
        The exception is ``ci_fix``, which is raised to at least
        :attr:`ci_fix_agent_budget_seconds`.

        Why the floor exists: ``ci_fix`` is the only stage that blocks on
        *external* CI, so its agent's budget is a product of two other
        settings and drifts independently of the stage wrapper. With the
        shipped defaults the agent was allowed 5 waits x 1500 s while the
        stage wrapper allowed 2400 s — the wrapper killed the agent at
        ~32% of its sanctioned budget, mid-verify-loop, discarding fixes
        it had already pushed. That produced 25 of the 31 stage timeouts
        this mill has ever recorded. Deriving the floor means the two
        numbers cannot silently disagree again.

        A deliberate 0 (timeout disabled) is always respected.
        """
        explicit = self.stage_timeout_overrides.get(stage_name)
        resolved = explicit if explicit is not None else int(self.stage_timeout_seconds)
        if resolved == 0:
            return 0  # explicitly disabled — respect it
        if stage_name == "ci_fix":
            return max(resolved, self.ci_fix_agent_budget_seconds)
        return resolved

    # -- cross-field checks --------------------------------------------

    @model_validator(mode="after")
    def _validate_cross_field(self) -> Settings:
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
            has_app_id = (
                self.github_app_id is not None and self.github_app_id.get_secret_value()
            )
            has_key_path = (
                self.github_app_private_key_path is not None
                and self.github_app_private_key_path.get_secret_value()
            )
            if not has_app_id and not has_key_path:
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
    """Load Settings from config/config.json via robotsix_config."""
    return robotsix_config.load_config(Settings)
