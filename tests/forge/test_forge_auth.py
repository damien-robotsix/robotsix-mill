import pytest
import robotsix_github_auth as rga

from robotsix_mill.config import Secrets, Settings, _reset_secrets
from robotsix_mill.forge import auth


def _set_secrets(**kw):
    """Populate the Secrets singleton for tests."""
    import robotsix_mill.config as _cfg

    _reset_secrets()
    _cfg._secrets = Secrets(**kw)


def S(tmp_path, **e):
    e.setdefault("data_dir", str(tmp_path))
    # Mirror secret fields into Secrets so get_secrets() works
    secrets_kw = {}
    for key in (
        "forge_token",
        "github_app_id",
        "github_app_private_key",
        "github_app_private_key_path",
    ):
        val = e.get(key.upper())
        if val is not None:
            secrets_kw[key] = val
    if secrets_kw:
        _set_secrets(**secrets_kw)
    # FORGE_TOKEN, GITHUB_APP_ID, and GITHUB_APP_PRIVATE_KEY are now
    # Secrets-only fields; pop before Settings()
    e.pop("FORGE_TOKEN", None)
    e.pop("GITHUB_APP_ID", None)
    e.pop("GITHUB_APP_PRIVATE_KEY", None)
    e.pop("GITHUB_APP_PRIVATE_KEY_PATH", None)
    s = Settings(**e)
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
    """github_token delegates to the library's github_token for app mode."""
    rga.clear_token_cache()
    calls = {"n": 0}

    def fake_github_token(**kwargs):
        calls["n"] += 1
        return "ghs_minted"

    monkeypatch.setattr(rga, "github_token", fake_github_token)
    s = S(
        tmp_path,
        FORGE_AUTH="app",
        GITHUB_APP_ID="123",
        GITHUB_APP_PRIVATE_KEY="KEY",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
    )
    assert auth.github_token(s) == "ghs_minted"
    # Caching is handled by the library internally; the mill layer
    # delegates every call through to the library.
    assert calls["n"] == 1


def test_private_key_from_path(tmp_path):
    p = tmp_path / "key.pem"
    p.write_text("-----BEGIN-----\nabc\n-----END-----\n")
    S(tmp_path, GITHUB_APP_PRIVATE_KEY_PATH=str(p))
    assert "abc" in auth._private_key()


# ---------------------------------------------------------------------------
# TokenMintError in classify_token_error
# ---------------------------------------------------------------------------


def test_classify_token_error_identifies_token_mint_error():
    """TokenMintError is classified as permanent."""
    assert auth.classify_token_error(rga.TokenMintError("test")) == "permanent"


def test_classify_missing_config_is_permanent():
    """RuntimeError about missing GitHub App config → permanent."""
    assert (
        auth.classify_token_error(RuntimeError("GITHUB_APP_ID not set")) == "permanent"
    )


def test_classify_missing_forge_token_is_permanent():
    """RuntimeError about missing FORGE_TOKEN → permanent."""
    assert auth.classify_token_error(RuntimeError("FORGE_TOKEN not set")) == "permanent"


def test_classify_connection_error_is_transient():
    """httpx.ConnectError → transient (network blip)."""
    import httpx

    assert (
        auth.classify_token_error(httpx.ConnectError("connection refused"))
        == "transient"
    )


def test_classify_timeout_is_transient():
    """httpx.TimeoutException → transient."""
    import httpx

    assert auth.classify_token_error(httpx.TimeoutException("timed out")) == "transient"


def test_classify_http_500_is_transient():
    """HTTP 500 → transient (server-side degradation)."""
    import httpx

    resp = httpx.Response(502)
    exc = httpx.HTTPStatusError(
        "Bad Gateway", request=httpx.Request("GET", "/"), response=resp
    )
    assert auth.classify_token_error(exc) == "transient"


def test_classify_http_400_is_permanent():
    """HTTP 400 → permanent (client error won't fix itself)."""
    import httpx

    resp = httpx.Response(400)
    exc = httpx.HTTPStatusError(
        "Bad Request", request=httpx.Request("GET", "/"), response=resp
    )
    assert auth.classify_token_error(exc) == "permanent"


def test_classify_jwt_error_is_permanent():
    """Exception with 'jwt' in message → permanent (bad key)."""
    assert (
        auth.classify_token_error(ValueError("jwt decode error: invalid signature"))
        == "permanent"
    )


def test_classify_unknown_exception_is_permanent():
    """Unknown exception type → permanent (conservative)."""
    assert auth.classify_token_error(ValueError("something unexpected")) == "permanent"


# ---------------------------------------------------------------------------
# invalidate_github_token
# ---------------------------------------------------------------------------


def test_invalidate_github_token_clears_cache(tmp_path, monkeypatch):
    """invalidate_github_token calls clear_token_cache on the library."""
    clear_calls = []

    def fake_clear():
        clear_calls.append(1)

    monkeypatch.setattr(rga, "clear_token_cache", fake_clear)

    s = S(
        tmp_path,
        FORGE_AUTH="app",
        GITHUB_APP_ID="111",
        GITHUB_APP_PRIVATE_KEY="K1",
        FORGE_REMOTE_URL="https://github.com/o1/r1.git",
    )
    auth.invalidate_github_token(s)
    assert len(clear_calls) == 1


# ---------------------------------------------------------------------------
# gitlab_token
# ---------------------------------------------------------------------------


def test_gitlab_token_returns_pat(tmp_path):
    """gitlab_token() returns the PAT from secrets when configured."""
    S(tmp_path, FORGE_TOKEN="glpat-mytoken")
    assert auth.gitlab_token() == "glpat-mytoken"


def test_gitlab_token_raises_when_not_set(tmp_path):
    """gitlab_token() raises RuntimeError when FORGE_TOKEN is not set."""
    # No FORGE_TOKEN → Secrets.forge_token is None
    S(tmp_path)
    with pytest.raises(RuntimeError, match="FORGE_TOKEN not set"):
        auth.gitlab_token()


# ---------------------------------------------------------------------------
# github_push_token
# ---------------------------------------------------------------------------


def test_push_token_pat_fallback(tmp_path):
    """When forge_auth != 'app', github_push_token falls back to the PAT."""
    assert auth.github_push_token(S(tmp_path, FORGE_TOKEN="pat123")) == "pat123"
