"""Runtime configuration, sourced from environment / .env.

Conventional keys (``OPENROUTER_API_KEY``, ``LANGFUSE_*``) are
unprefixed to match the reference projects; mill-specific knobs use the
``MILL_`` / ``FORGE_`` prefixes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- core ---
    openrouter_api_key: str | None = Field(default=None, alias="OPENROUTER_API_KEY")
    model: str = Field(default="anthropic/claude-sonnet-4-6", alias="MILL_MODEL")
    # Local-dev default: a repo-local, gitignored dir. The Dockerfile
    # sets MILL_DATA_DIR=/data explicitly, so the container is unaffected.
    data_dir: Path = Field(default=Path(".mill-data"), alias="MILL_DATA_DIR")

    # --- management-plane service ---
    api_host: str = Field(default="127.0.0.1", alias="MILL_API_HOST")
    api_port: int = Field(default=8077, alias="MILL_API_PORT")
    # Base URL the CLI client talks to.
    api_url: str = Field(default="http://127.0.0.1:8077", alias="MILL_API_URL")

    # --- forge delivery (only used by the deliver stage) ---
    forge_kind: Literal["github", "gitlab", "none"] = Field(
        default="none", alias="FORGE_KIND"
    )
    forge_remote_url: str | None = Field(default=None, alias="FORGE_REMOTE_URL")
    forge_token: str | None = Field(default=None, alias="FORGE_TOKEN")
    forge_target_branch: str = Field(default="main", alias="FORGE_TARGET_BRANCH")
    # token  = use FORGE_TOKEN (PAT) directly.
    # app    = mint a short-lived GitHub App installation token so the
    #          bot identity (<app-slug>[bot]) authors the PR.
    forge_auth: Literal["token", "app"] = Field(
        default="token", alias="FORGE_AUTH"
    )
    github_app_id: str | None = Field(default=None, alias="GITHUB_APP_ID")
    github_app_private_key: str | None = Field(
        default=None, alias="GITHUB_APP_PRIVATE_KEY"
    )
    github_app_private_key_path: str | None = Field(
        default=None, alias="GITHUB_APP_PRIVATE_KEY_PATH"
    )
    # GitHub API base (override for GitHub Enterprise).
    github_api_url: str = Field(
        default="https://api.github.com", alias="MILL_GITHUB_API_URL"
    )

    # --- implement stage ---
    # Command run to verify the implementation; empty string skips the
    # test gate. Failures feed back into the bounded fix loop.
    test_command: str = Field(default="pytest -q", alias="MILL_TEST_COMMAND")
    max_fix_attempts: int = Field(default=3, alias="MILL_MAX_FIX_ATTEMPTS")
    branch_prefix: str = Field(default="mill/", alias="MILL_BRANCH_PREFIX")
    # Max model requests per implement agent pass (pydantic-ai default is
    # 50 — far too low for an agentic coding loop on a real repo).
    agent_request_limit: int = Field(
        default=200, alias="MILL_AGENT_REQUEST_LIMIT"
    )
    # Wall-clock cap (seconds) for the agent's shell tool and the test
    # command, so a hung command can't stall a worker forever.
    command_timeout: int = Field(default=600, alias="MILL_COMMAND_TIMEOUT")

    # --- command sandbox (always a disposable container; no local mode) ---
    # Image the sandbox runs commands in — must contain the toolchain
    # MILL_TEST_COMMAND needs.
    sandbox_image: str = Field(
        default="python:3.14-slim", alias="MILL_SANDBOX_IMAGE"
    )
    sandbox_memory: str = Field(default="2g", alias="MILL_SANDBOX_MEMORY")
    sandbox_pids_limit: int = Field(
        default=512, alias="MILL_SANDBOX_PIDS_LIMIT"
    )
    sandbox_readonly: bool = Field(
        default=True, alias="MILL_SANDBOX_READONLY"
    )
    # What the sandbox sibling containers mount at MILL_DATA_DIR. The
    # daemon resolves -v on the host, so this must be a named volume OR
    # the host path of a bind mount. data_volume is the fallback name;
    # sandbox_data_mount (host path) overrides it for bind-mounted ./.data.
    data_volume: str = Field(default="mill_data", alias="MILL_DATA_VOLUME")
    sandbox_data_mount: str | None = Field(
        default=None, alias="MILL_SANDBOX_DATA_MOUNT"
    )

    # --- agent web access (refine + implement) ---
    # OpenRouter server-side web search via the ":online" model suffix.
    web_search: bool = Field(default=True, alias="MILL_WEB_SEARCH")
    # web_fetch runs in its OWN container: network ON, but NO repo/data
    # mount, non-root, read-only, fixed curl. Trade-off accepted: an
    # agent could encode data into a fetched URL. http(s) only.
    fetch_image: str = Field(
        default="curlimages/curl:latest", alias="MILL_FETCH_IMAGE"
    )
    web_fetch_max_bytes: int = Field(
        default=2_000_000, alias="MILL_WEB_FETCH_MAX_BYTES"
    )
    web_fetch_timeout: int = Field(
        default=30, alias="MILL_WEB_FETCH_TIMEOUT"
    )
    # Directory of skill docs (skills/<name>/SKILL.md) injected into the
    # refine + implement agents' system prompt. Relative to CWD (/app in
    # the container, repo root locally).
    skills_dir: Path = Field(default=Path("skills"), alias="MILL_SKILLS_DIR")

    # --- retrospect stage (done -> reviewed) ---
    # When True, retrospect may file an improvement DRAFT. Until the
    # human-gate-after-refine exists, that draft auto-flows to done and
    # is retrospected again — set False to analyse without spawning.
    retrospect_spawn_drafts: bool = Field(
        default=True, alias="MILL_RETROSPECT_SPAWN_DRAFTS"
    )
    # in_review (PR open) re-check cadence. mill has no scheduler; this
    # timer exists only to observe the external merge event.
    merge_poll_seconds: int = Field(
        default=120, alias="MILL_MERGE_POLL_SECONDS"
    )

    # --- tracing (optional) ---
    langfuse_base_url: str | None = Field(default=None, alias="LANGFUSE_BASE_URL")
    langfuse_public_key: str | None = Field(default=None, alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str | None = Field(default=None, alias="LANGFUSE_SECRET_KEY")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mill.db"

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    @property
    def tracing_enabled(self) -> bool:
        return bool(
            self.langfuse_base_url
            and self.langfuse_public_key
            and self.langfuse_secret_key
        )


def load_settings() -> Settings:
    return Settings()
