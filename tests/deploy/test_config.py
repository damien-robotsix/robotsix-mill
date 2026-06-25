"""Tests for the deploy-server configuration module."""

from __future__ import annotations

import os

from robotsix_deploy.config import DeploySettings


def test_defaults() -> None:
    """DeploySettings uses sensible defaults when no env vars are set."""
    # Clear any DEPLOY_* env vars that might leak from the test environment.
    for key in list(os.environ):
        if key.startswith("DEPLOY_"):
            del os.environ[key]

    settings = DeploySettings()
    assert settings.host == "127.0.0.1"
    assert settings.port == 8080
    assert settings.log_level == "info"
    assert settings.broker_url == ""
    assert settings.langfuse_host == ""
    assert settings.langfuse_public_key == ""
    assert settings.langfuse_secret_key == ""


def test_env_override(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """DeploySettings reads from DEPLOY_* env vars."""
    monkeypatch.setenv("DEPLOY_HOST", "0.0.0.0")
    monkeypatch.setenv("DEPLOY_PORT", "9090")
    monkeypatch.setenv("DEPLOY_LOG_LEVEL", "debug")
    monkeypatch.setenv("DEPLOY_BROKER_URL", "redis://localhost:6379")
    monkeypatch.setenv("DEPLOY_LANGFUSE_HOST", "https://langfuse.example.com")
    monkeypatch.setenv("DEPLOY_LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("DEPLOY_LANGFUSE_SECRET_KEY", "sk-test")

    settings = DeploySettings()
    assert settings.host == "0.0.0.0"
    assert settings.port == 9090
    assert settings.log_level == "debug"
    assert settings.broker_url == "redis://localhost:6379"
    assert settings.langfuse_host == "https://langfuse.example.com"
    assert settings.langfuse_public_key == "pk-test"
    assert settings.langfuse_secret_key == "sk-test"


def test_unknown_env_var_ignored(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """DeploySettings silently ignores unknown DEPLOY_* env vars."""
    monkeypatch.setenv("DEPLOY_UNKNOWN_FIELD", "value")
    settings = DeploySettings()
    assert settings.host == "127.0.0.1"  # still uses defaults
