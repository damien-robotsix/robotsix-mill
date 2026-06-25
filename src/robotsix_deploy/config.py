"""Env-based configuration (12-factor) for the deploy server.

Reads service port, log level, and placeholders for downstream
integrations (broker, Langfuse) that sibling tickets will wire.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DeploySettings(BaseSettings):
    """Configuration for the central deployment & lifecycle server.

    All fields are sourced from environment variables.  The ``DEPLOY_``
    prefix avoids collisions with the mill management-plane settings.
    """

    model_config = SettingsConfigDict(
        env_prefix="DEPLOY_",
        extra="forbid",
    )

    # --- service ---
    host: str = Field(default="127.0.0.1")
    port: int = Field(default=8080, ge=1, le=65535)
    log_level: str = Field(default="info")

    # --- downstream integration placeholders (wired by sibling tickets) ---
    # Message broker for async task dispatch (e.g. Redis, NATS).
    broker_url: str = Field(default="")
    # Langfuse observability (host + public/secret key pair).
    langfuse_host: str = Field(default="")
    langfuse_public_key: str = Field(default="")
    langfuse_secret_key: str = Field(default="")
