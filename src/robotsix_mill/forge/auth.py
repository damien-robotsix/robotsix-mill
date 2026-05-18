"""Resolve the effective GitHub token for push + PR.

``FORGE_AUTH=token`` → use ``FORGE_TOKEN`` (a PAT) as-is.
``FORGE_AUTH=app``   → mint a short-lived GitHub App *installation*
access token (JWT signed with the App private key → installation
token), so the PR is authored by ``<app-slug>[bot]`` — the
robotsix-project bot identity, without GitHub Actions.

``_mint_installation_token`` is the network/JWT seam (monkeypatched in
tests, so pyjwt/httpx aren't needed for the token-auth path or the
suite). Minted tokens (~1 h TTL) are cached so one deliver doesn't mint
twice (push + PR).
"""

from __future__ import annotations

import time

from ..config import Settings
from .github import _parse_owner_repo

_cache: dict[str, tuple[str, float]] = {}


def _private_key(settings: Settings) -> str:
    if settings.github_app_private_key_path:
        with open(settings.github_app_private_key_path, encoding="utf-8") as f:
            return f.read()
    key = settings.github_app_private_key or ""
    # allow a single-line env value with literal "\n"
    return key.replace("\\n", "\n")


def _mint_installation_token(settings: Settings) -> tuple[str, float]:
    """Returns (token, unix_expiry). Seam: tests monkeypatch this."""
    import httpx
    import jwt

    api = settings.github_api_url.rstrip("/")
    owner, repo = _parse_owner_repo(settings.forge_remote_url or "")
    now = int(time.time())
    bearer = jwt.encode(
        {"iat": now - 60, "exp": now + 9 * 60, "iss": settings.github_app_id},
        _private_key(settings),
        algorithm="RS256",
    )
    h = {
        "Authorization": f"Bearer {bearer}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    with httpx.Client(timeout=30) as c:
        inst = c.get(f"{api}/repos/{owner}/{repo}/installation", headers=h)
        inst.raise_for_status()
        iid = inst.json()["id"]
        tok = c.post(
            f"{api}/app/installations/{iid}/access_tokens", headers=h
        )
        tok.raise_for_status()
        data = tok.json()
    # expires_at is ISO; just cache for 50 min regardless
    return data["token"], time.time() + 50 * 60


def github_token(settings: Settings) -> str:
    if settings.forge_auth != "app":
        if not settings.forge_token:
            raise RuntimeError("FORGE_TOKEN not set")
        return settings.forge_token

    if not settings.github_app_id or not (
        settings.github_app_private_key
        or settings.github_app_private_key_path
    ):
        raise RuntimeError(
            "FORGE_AUTH=app needs GITHUB_APP_ID and "
            "GITHUB_APP_PRIVATE_KEY[_PATH]"
        )
    ck = f"{settings.github_app_id}:{settings.forge_remote_url}"
    cached = _cache.get(ck)
    if cached and cached[1] - 60 > time.time():
        return cached[0]
    token, expiry = _mint_installation_token(settings)
    _cache[ck] = (token, expiry)
    return token
