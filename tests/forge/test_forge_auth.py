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
    # FORGE_TOKEN and GITHUB_APP_PRIVATE_KEY are now Secrets-only fields;
    # pop before Settings()
    e.pop("FORGE_TOKEN", None)
    e.pop("GITHUB_APP_PRIVATE_KEY", None)
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
    auth._cache.clear()
    calls = {"n": 0}

    def fake_mint(settings, repo_config=None):
        calls["n"] += 1
        return "ghs_minted", time.time() + 3000

    monkeypatch.setattr(auth, "_mint_installation_token", fake_mint)
    s = S(
        tmp_path,
        FORGE_AUTH="app",
        GITHUB_APP_ID="123",
        GITHUB_APP_PRIVATE_KEY="KEY",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
    )
    assert auth.github_token(s) == "ghs_minted"
    assert auth.github_token(s) == "ghs_minted"  # served from cache
    assert calls["n"] == 1  # minted once, not twice


def test_private_key_from_path(tmp_path):
    p = tmp_path / "key.pem"
    p.write_text("-----BEGIN-----\nabc\n-----END-----\n")
    S(tmp_path, GITHUB_APP_PRIVATE_KEY_PATH=str(p))
    assert "abc" in auth._private_key()


# ---------------------------------------------------------------------------
# GitHubAppNotInstalledError
# ---------------------------------------------------------------------------


def test_mint_installation_token_raises_on_404(tmp_path, monkeypatch):
    """_mint_installation_token raises GitHubAppNotInstalledError when
    the /installation endpoint returns 404 (App not installed)."""
    import httpx
    import jwt as jwt_module

    def fake_get(self, url, **kwargs):
        return httpx.Response(404, json={"message": "Not Found"})

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    monkeypatch.setattr(auth, "_private_key", lambda: "fake-key")
    # Bypass JWT encode — we only care about the HTTP response handling.
    monkeypatch.setattr(jwt_module, "encode", lambda *a, **kw: "fake-jwt")
    # _parse_owner_repo needs a valid remote
    s = S(
        tmp_path,
        FORGE_AUTH="app",
        GITHUB_APP_ID="123",
        GITHUB_APP_PRIVATE_KEY="KEY",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
    )
    with pytest.raises(auth.GitHubAppNotInstalledError) as exc:
        auth._mint_installation_token(s)
    assert exc.value.owner == "o"
    assert exc.value.repo == "r"


# ---------------------------------------------------------------------------
# invalidate_github_token
# ---------------------------------------------------------------------------


def test_invalidate_github_token_removes_correct_entry(tmp_path, monkeypatch):
    """Populate cache with two entries, invalidate one, verify the other
    survives."""
    auth._cache.clear()
    mint_calls = []

    def fake_mint(settings, repo_config=None):
        mint_calls.append(1)
        return f"tok_{len(mint_calls)}", time.time() + 3000

    monkeypatch.setattr(auth, "_mint_installation_token", fake_mint)

    # Two settings that share the same github_app_id (global secrets
    # singleton) but differ in remote_url — so the cache-key
    # namespace (app_id:remote_url) yields two distinct entries.
    s1 = S(
        tmp_path,
        FORGE_AUTH="app",
        GITHUB_APP_ID="111",
        GITHUB_APP_PRIVATE_KEY="K1",
        FORGE_REMOTE_URL="https://github.com/o1/r1.git",
    )
    s2 = S(
        tmp_path,
        FORGE_AUTH="app",
        GITHUB_APP_ID="111",
        GITHUB_APP_PRIVATE_KEY="K1",
        FORGE_REMOTE_URL="https://github.com/o2/r2.git",
    )

    # Populate two distinct cache entries.
    assert auth.github_token(s1) == "tok_1"
    assert auth.github_token(s2) == "tok_2"
    assert len(auth._cache) == 2

    # Invalidate s1; s2's entry must remain.
    auth.invalidate_github_token(s1)
    assert len(auth._cache) == 1
    # s2 entry still present
    ck2 = "111:https://github.com/o2/r2.git"
    assert ck2 in auth._cache

    # Next call for s1 mints a fresh token (cache miss → mint again).
    tok3 = auth.github_token(s1)
    assert tok3 == "tok_3"  # fresh mint
    assert len(auth._cache) == 2


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


def test_push_token_app_mode_mints_fresh_with_contents_write(tmp_path, monkeypatch):
    """github_push_token mints a fresh token with contents:write permission
    and does NOT cache — each call issues a fresh mint."""
    import httpx
    import jwt as jwt_module

    auth._cache.clear()
    mint_calls = []

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, headers=None, **kwargs):
            mint_calls.append(("get", url))
            return httpx.Response(200, json={"id": 42})

        def post(self, url, headers=None, json=None, **kwargs):
            mint_calls.append(("post", url, json))
            return httpx.Response(
                201,
                json={"token": "ghs_push_minted", "expires_at": "..."},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(httpx, "Client", FakeClient)
    monkeypatch.setattr(auth, "_private_key", lambda: "fake-key")
    monkeypatch.setattr(jwt_module, "encode", lambda *a, **kw: "fake-jwt")

    s = S(
        tmp_path,
        FORGE_AUTH="app",
        GITHUB_APP_ID="123",
        GITHUB_APP_PRIVATE_KEY="KEY",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
    )

    # Two calls → two mints (no caching).
    t1 = auth.github_push_token(s)
    t2 = auth.github_push_token(s)
    assert t1 == "ghs_push_minted"
    assert t2 == "ghs_push_minted"
    assert len(mint_calls) == 4  # 2 × (GET installation + POST access_token)

    # Verify the POST body includes contents:write + repositories.
    post_bodies = [m[2] for m in mint_calls if m[0] == "post"]
    assert len(post_bodies) == 2
    for body in post_bodies:
        assert body == {"permissions": {"contents": "write"}, "repositories": ["r"]}


def test_push_token_not_installed_raises(tmp_path, monkeypatch):
    """github_push_token raises GitHubAppNotInstalledError when the App
    is not installed on the target repo (404 from /installation)."""
    import httpx
    import jwt as jwt_module

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, headers=None, **kwargs):
            return httpx.Response(404, json={"message": "Not Found"})

    monkeypatch.setattr(httpx, "Client", FakeClient)
    monkeypatch.setattr(auth, "_private_key", lambda: "fake-key")
    monkeypatch.setattr(jwt_module, "encode", lambda *a, **kw: "fake-jwt")

    s = S(
        tmp_path,
        FORGE_AUTH="app",
        GITHUB_APP_ID="123",
        GITHUB_APP_PRIVATE_KEY="KEY",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
    )
    with pytest.raises(auth.GitHubAppNotInstalledError) as exc:
        auth.github_push_token(s)
    assert exc.value.owner == "o"
    assert exc.value.repo == "r"


def test_push_token_requires_app_config(tmp_path):
    """github_push_token raises RuntimeError when FORGE_AUTH=app but
    no GitHub App credentials are configured."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="FORGE_AUTH=app requires"):
        auth.github_push_token(S(tmp_path, FORGE_AUTH="app"))


def test_build_app_jwt_returns_valid_jwt(tmp_path):
    """_build_app_jwt returns a JWT string with the app id as issuer."""
    import jwt as jwt_module

    monkeypatch = pytest.MonkeyPatch()
    S(
        tmp_path,
        GITHUB_APP_ID="456",
        GITHUB_APP_PRIVATE_KEY="test-key",
    )

    # Let _build_app_jwt run for real — need a real key to sign.
    # Since we don't have a real RSA key, monkeypatch jwt.encode
    # to return a predictable token.
    monkeypatch.setattr(jwt_module, "encode", lambda *a, **kw: "fake-jwt")
    token = auth._build_app_jwt(S(tmp_path))
    assert token == "fake-jwt"
