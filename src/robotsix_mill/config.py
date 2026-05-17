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

    # --- implement stage ---
    # Command run to verify the implementation; empty string skips the
    # test gate. Failures feed back into the bounded fix loop.
    test_command: str = Field(default="pytest -q", alias="MILL_TEST_COMMAND")
    max_fix_attempts: int = Field(default=3, alias="MILL_MAX_FIX_ATTEMPTS")
    branch_prefix: str = Field(default="mill/", alias="MILL_BRANCH_PREFIX")
    # Wall-clock cap (seconds) for the agent's shell tool and the test
    # command, so a hung command can't stall a worker forever.
    command_timeout: int = Field(default=600, alias="MILL_COMMAND_TIMEOUT")

    # --- command sandbox ---
    # docker: each command runs in a fresh disposable sibling container
    #   (isolated, no network). Requires the Docker socket in the mill
    #   container. local: in-process shell — NOT isolated, dev/CI only.
    sandbox_mode: Literal["docker", "local"] = Field(
        default="docker", alias="MILL_SANDBOX_MODE"
    )
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
    # Name of the volume mounted at MILL_DATA_DIR on the mill container;
    # the sandbox mounts it by name so workspace paths line up (see
    # sandbox.py). Only used in docker mode.
    data_volume: str = Field(default="mill_data", alias="MILL_DATA_VOLUME")

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
