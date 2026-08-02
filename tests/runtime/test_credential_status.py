"""Unit tests for the board's missing-credential self-check."""

from __future__ import annotations

import pytest

from robotsix_mill.config import Settings
from robotsix_mill.config.secrets import _reset_secrets
from robotsix_mill.runtime.credential_status import get_credential_status


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    """Point the config file at a per-test JSON and return a writer for it."""

    def _write(secrets: dict[str, str]) -> None:
        import json

        path = tmp_path / "config.json"
        path.write_text(json.dumps({"secrets": secrets}))
        monkeypatch.setenv("ROBOTSIX_CONFIG_FILE", str(path))
        _reset_secrets()

    return _write


_FULL_APP_CREDS = {
    "openrouter_api_key": "sk-or-test",
    "github_app_id": "12345",
    "github_app_private_key": "-----BEGIN RSA PRIVATE KEY-----",
}


def _settings(**kwargs) -> Settings:
    """Build a Settings without running its validators.

    ``Settings`` has its own App-mode validator over the *Settings*-level
    ``github_app_*`` fields, which would refuse to construct exactly the
    configurations under test here.  Those fields are a separate surface
    from the ``secrets:`` block that ``forge.auth`` actually reads via
    ``get_secrets()`` — which is what this check mirrors — so bypassing
    validation keeps the two from being conflated.
    """
    return Settings.model_construct(
        forge_remote_url="https://github.com/o/r.git", **kwargs
    )


def test_ok_when_all_required_creds_present(cfg):
    """No findings when OpenRouter and GitHub App credentials are set."""
    cfg(_FULL_APP_CREDS)
    status = get_credential_status(_settings(forge_auth="app"))
    assert status == {"ok": True, "missing": []}


def test_missing_openrouter_key_is_reported(cfg):
    """The OpenRouter key is required — without it no ticket can advance."""
    cfg({k: v for k, v in _FULL_APP_CREDS.items() if k != "openrouter_api_key"})
    status = get_credential_status(_settings(forge_auth="app"))
    assert status["ok"] is False
    assert [m["name"] for m in status["missing"]] == ["openrouter_api_key"]
    assert status["missing"][0]["config_path"] == "secrets.openrouter_api_key"


def test_missing_app_id_is_reported(cfg):
    """App mode needs the app id even when the private key is present."""
    cfg({k: v for k, v in _FULL_APP_CREDS.items() if k != "github_app_id"})
    status = get_credential_status(_settings(forge_auth="app"))
    assert [m["name"] for m in status["missing"]] == ["github_app"]
    assert status["missing"][0]["config_path"] == "secrets.github_app_id"


def test_missing_app_key_is_reported(cfg):
    """App mode needs a private key (inline or path) even with an app id."""
    cfg({"openrouter_api_key": "sk-or-test", "github_app_id": "12345"})
    status = get_credential_status(_settings(forge_auth="app"))
    assert status["missing"][0]["config_path"] == (
        "secrets.github_app_private_key[_path]"
    )


def test_private_key_path_satisfies_app_mode(cfg):
    """A key *path* is an accepted alternative to an inline key."""
    cfg(
        {
            "openrouter_api_key": "sk-or-test",
            "github_app_id": "12345",
            "github_app_private_key_path": "/app/config/key.pem",
        }
    )
    assert get_credential_status(_settings(forge_auth="app"))["ok"] is True


def test_blank_config_reports_every_required_cred(cfg):
    """The regression that started this: no secrets at all, both reported."""
    cfg({})
    status = get_credential_status(_settings(forge_auth="app"))
    assert status["ok"] is False
    assert sorted(m["name"] for m in status["missing"]) == [
        "github_app",
        "openrouter_api_key",
    ]
    assert status["missing"][1]["config_path"] == (
        "secrets.github_app_id and github_app_private_key[_path]"
    )


def test_pat_mode_requires_forge_token(cfg):
    """In PAT mode the App credentials are irrelevant; forge_token is not."""
    cfg({"openrouter_api_key": "sk-or-test", **_FULL_APP_CREDS})
    status = get_credential_status(_settings(forge_auth="token"))
    assert [m["name"] for m in status["missing"]] == ["forge_token"]


def test_pat_mode_ok_with_forge_token(cfg):
    """PAT mode is satisfied by forge_token alone."""
    cfg({"openrouter_api_key": "sk-or-test", "forge_token": "ghp-x"})
    assert get_credential_status(_settings(forge_auth="token"))["ok"] is True


def test_optional_creds_never_reported(cfg):
    """Langfuse / ntfy / management keys degrade a feature, not the pipeline."""
    cfg(_FULL_APP_CREDS)
    status = get_credential_status(_settings(forge_auth="app"))
    assert status["ok"] is True
