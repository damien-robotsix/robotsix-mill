"""Unit tests for the board's missing-credential self-check."""

from __future__ import annotations

import json
import time

import pytest

from robotsix_mill.config import Settings
from robotsix_mill.config.secrets import _reset_secrets
from robotsix_mill.runtime.credential_status import get_credential_status


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    """Point the config file at a per-test JSON and return a writer for it."""

    def _write(secrets: dict[str, str]) -> None:
        # The OpenRouter key lives in the canonical openrouter.keys
        # block, never as a flat secrets entry.
        secrets = dict(secrets)
        data: dict = {}
        or_key = secrets.pop("openrouter_api_key", None)
        if or_key:
            data["openrouter"] = {"keys": {"robotsix-mill": or_key}}
        data["secrets"] = secrets
        path = tmp_path / "config.json"
        path.write_text(json.dumps(data))
        monkeypatch.setenv("ROBOTSIX_CONFIG_FILE", str(path))
        # Pin the Claude CLI config dir to the test sandbox so the
        # stale-OAuth probe never reads a real ~/.claude on the host.
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
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
    assert status["missing"][0]["config_path"] == "openrouter.keys.robotsix-mill"


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


def test_private_key_path_satisfies_app_mode(cfg, tmp_path):
    """A key *path* is an accepted alternative to an inline key."""
    key_file = tmp_path / "key.pem"
    key_file.write_text(
        "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----"
    )
    cfg(
        {
            "openrouter_api_key": "sk-or-test",
            "github_app_id": "12345",
            "github_app_private_key_path": str(key_file),
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


def test_invalid_inline_key_is_reported(cfg):
    """A key that is present but not valid PEM is flagged as misconfigured."""
    cfg(
        {
            "openrouter_api_key": "sk-or-test",
            "github_app_id": "12345",
            "github_app_private_key": "not-a-valid-key",
        }
    )
    status = get_credential_status(_settings(forge_auth="app"))
    assert status["ok"] is False
    assert status["missing"][0]["name"] == "github_app_private_key"
    assert "not valid PEM" in status["missing"][0]["impact"]


def test_unreadable_key_path_is_reported(cfg):
    """A key path that cannot be opened is flagged as misconfigured."""
    cfg(
        {
            "openrouter_api_key": "sk-or-test",
            "github_app_id": "12345",
            "github_app_private_key_path": "/nonexistent/key.pem",
        }
    )
    status = get_credential_status(_settings(forge_auth="app"))
    assert status["ok"] is False
    assert status["missing"][0]["name"] == "github_app_private_key_path"
    assert "cannot be read" in status["missing"][0]["impact"]


def test_valid_key_path_with_pem_is_ok(cfg, tmp_path):
    """A readable key file with valid PEM content is accepted."""
    key_file = tmp_path / "app-key.pem"
    key_file.write_text(
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----"
    )
    cfg(
        {
            "openrouter_api_key": "sk-or-test",
            "github_app_id": "12345",
            "github_app_private_key_path": str(key_file),
        }
    )
    assert get_credential_status(_settings(forge_auth="app"))["ok"] is True


# ---------------------------------------------------------------------------
# Stale Claude CLI OAuth detection
# ---------------------------------------------------------------------------


def _write_claude_credentials(tmp_path, payload: dict) -> None:
    (tmp_path / ".credentials.json").write_text(json.dumps(payload), encoding="utf-8")


def test_expired_claude_oauth_is_reported(cfg, tmp_path):
    """A present-but-expired OAuth session is flagged as a stale credential.

    Without this the dead Claude session is only discovered at call time,
    when every default-slot tier attempt raises ClaudeSDKAuthError and the
    pass silently completes downgraded on the fallback slot.
    """
    cfg(_FULL_APP_CREDS)
    _write_claude_credentials(
        tmp_path,
        {
            "claudeAiOauth": {
                "accessToken": "sk-ant-test",
                "expiresAt": 1_000_000_000_000,
            }
        },
    )
    status = get_credential_status(_settings(forge_auth="app"))
    assert status["ok"] is False
    finding = next(m for m in status["missing"] if m["name"] == "claude_oauth_expired")
    assert finding["state"] == "expired"
    assert ".credentials.json" in finding["config_path"]
    assert "claude login" in finding["impact"]


def test_valid_claude_oauth_is_not_reported(cfg, tmp_path):
    """A session whose token has not expired yet is left alone."""
    cfg(_FULL_APP_CREDS)
    _write_claude_credentials(
        tmp_path,
        {
            "claudeAiOauth": {
                "accessToken": "sk-ant-test",
                "expiresAt": int(time.time() * 1000) + 3_600_000,
            }
        },
    )
    assert get_credential_status(_settings(forge_auth="app"))["ok"] is True


def test_missing_claude_oauth_file_is_not_reported(cfg):
    """No local session is not a finding — OAuth may live in the macOS
    Keychain, and machines that never use the Claude slot must not trip
    the banner."""
    cfg(_FULL_APP_CREDS)
    assert get_credential_status(_settings(forge_auth="app"))["ok"] is True


def test_unrecognized_claude_credentials_are_not_reported(cfg, tmp_path):
    """Unknown file structure (no accessToken / no expiresAt) is not
    treated as a stale session."""
    for payload in ({"weird": True}, {"claudeAiOauth": {"accessToken": "sk-ant-x"}}):
        cfg(_FULL_APP_CREDS)
        _write_claude_credentials(tmp_path, payload)
        assert get_credential_status(_settings(forge_auth="app"))["ok"] is True


def test_corrupt_claude_credentials_file_is_not_reported(cfg, tmp_path):
    """An unreadable / non-JSON store is treated as no finding."""
    cfg(_FULL_APP_CREDS)
    (tmp_path / ".credentials.json").write_text("{not json", encoding="utf-8")
    status = get_credential_status(_settings(forge_auth="app"))
    assert status["ok"] is True
