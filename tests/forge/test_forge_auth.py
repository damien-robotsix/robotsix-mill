import time

import pytest

from robotsix_mill.config import Settings, Secrets, _reset_secrets
from robotsix_mill.forge import auth


def _set_secrets(**kw):
    """Populate the Secrets singleton for tests."""
    import robotsix_mill.config as _cfg
    _reset_secrets()
    _cfg._secrets = Secrets(**kw)


def S(tmp_path, **e):
    e.setdefault("MILL_DATA_DIR", str(tmp_path))
    s = Settings(**e)
    # Mirror secret fields into Secrets so get_secrets() works
    secrets_kw = {}
    for key in ("forge_token", "github_app_id", "github_app_private_key",
                "github_app_private_key_path"):
        val = e.get(key.upper())
        if val is not None:
            secrets_kw[key] = val
    if secrets_kw:
        _set_secrets(**secrets_kw)
    return s


def test_token_mode_returns_pat(tmp_path):
    assert auth.github_token(S(tmp_path, FORGE_TOKEN="pat123")) == "pat123"


def test_token_mode_requires_pat(tmp_path):
    with pytest.raises(RuntimeError, match="FORGE_TOKEN"):
        auth.github_token(S(tmp_path))


def test_app_mode_requires_app_config(tmp_path):
    # The cross-field validator now catches this at Settings
    # construction time (ValidationError), before the auth module
    # has a chance to raise RuntimeError.
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="FORGE_AUTH=app requires"):
        S(tmp_path, FORGE_AUTH="app")


def test_app_mode_mints_and_caches(tmp_path, monkeypatch):
    auth._cache.clear()
    calls = {"n": 0}

    def fake_mint(settings, repo_config=None):
        calls["n"] += 1
        return "ghs_minted", time.time() + 3000

    monkeypatch.setattr(auth, "_mint_installation_token", fake_mint)
    s = S(
        tmp_path, FORGE_AUTH="app", GITHUB_APP_ID="123",
        GITHUB_APP_PRIVATE_KEY="KEY",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
    )
    assert auth.github_token(s) == "ghs_minted"
    assert auth.github_token(s) == "ghs_minted"  # served from cache
    assert calls["n"] == 1  # minted once, not twice


def test_private_key_from_path(tmp_path):
    p = tmp_path / "key.pem"
    p.write_text("-----BEGIN-----\nabc\n-----END-----\n")
    s = S(tmp_path, GITHUB_APP_PRIVATE_KEY_PATH=str(p))
    assert "abc" in auth._private_key()
