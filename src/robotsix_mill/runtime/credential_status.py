"""Credential self-check for the board's missing-credentials banner.

When a credential mill needs is absent, every ticket that reaches a stage
requiring it dies with a per-ticket ``RuntimeError`` and lands in
``blocked``.  Nothing said *why* at the board level, so a config
regression that blanked every secret (#2525 left ``Secrets`` with no file
source at all) looked like a wave of unrelated ticket failures rather than
one fleet-wide cause.

This module answers "can mill work at all?" by reading the live
:func:`~robotsix_mill.config.get_secrets` view and reporting which
required credentials are missing.  It is computed on demand — no cached
state — so an operator who fixes ``config.json`` and restarts sees the
banner clear on the next poll.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings, get_secrets


def _openrouter_missing() -> dict[str, str] | None:
    """Return a finding when the OpenRouter API key is unset.

    Without it every LLM-backed stage raises ``OPENROUTER_API_KEY is not
    set`` before making a call, so no ticket can advance.
    """
    if get_secrets().openrouter_api_key:
        return None
    return {
        "name": "openrouter_api_key",
        "config_path": "secrets.openrouter_api_key",
        "impact": "no LLM stage can run — every ticket will block",
    }


def _forge_missing(settings: Settings) -> dict[str, str] | None:
    """Return a finding when forge authentication cannot be assembled.

    Mirrors the checks in :func:`robotsix_mill.forge.auth.github_token`:
    App mode needs an app id plus a key (inline or path); PAT mode needs
    ``forge_token``.
    """
    secrets = get_secrets()
    if settings.forge_auth == "app":
        has_key = bool(
            secrets.github_app_private_key or secrets.github_app_private_key_path
        )
        if secrets.github_app_id and has_key:
            # Credentials are present — validate the key is actually
            # loadable so a misconfigured key (garbage PEM, unreadable
            # file) is caught at startup rather than at merge time.
            key_finding = _validate_app_private_key()
            if key_finding is not None:
                return key_finding
            return None
        if not secrets.github_app_id and not has_key:
            missing = "github_app_id and github_app_private_key[_path]"
        elif not secrets.github_app_id:
            missing = "github_app_id"
        else:
            missing = "github_app_private_key[_path]"
        return {
            "name": "github_app",
            "config_path": f"secrets.{missing}",
            "impact": (
                "forge_auth=app cannot mint an installation token — "
                "no clone, push, PR or CI polling"
            ),
        }
    if secrets.forge_token:
        return None
    return {
        "name": "forge_token",
        "config_path": "secrets.forge_token",
        "impact": f"forge_auth={settings.forge_auth} has no token — no push or PR",
    }


def _validate_app_private_key() -> dict[str, str] | None:
    """Validate the GitHub App private key can actually be loaded.

    Returns a finding when the key is present but unreadable or not valid
    PEM, or ``None`` when the key loads successfully (or isn't configured
    — presence is checked separately by :func:`_forge_missing`).

    A misconfigured key that passes the presence check but fails at JWT
    signing time is the most common "why are all my tickets blocked?"
    regression — the banner catches it at startup rather than one
    ``RuntimeError`` per merge-stage ticket.
    """
    secrets = get_secrets()
    key_data: str | None = None
    try:
        if secrets.github_app_private_key_path:
            with open(secrets.github_app_private_key_path, encoding="utf-8") as f:
                key_data = f.read()
        elif secrets.github_app_private_key:
            key_data = secrets.github_app_private_key.replace("\\n", "\n")
        else:
            return None  # key not configured — presence check handles this

        # Basic PEM validation: a valid RSA/EC private key always has the
        # BEGIN/END markers.  Garbage strings, JSON, or Base64-encoded
        # blobs won't.
        if "-----BEGIN" not in key_data or "PRIVATE KEY-----" not in key_data:
            return {
                "name": "github_app_private_key",
                "config_path": "secrets.github_app_private_key[_path]",
                "impact": (
                    "forge_auth=app private key is not valid PEM — "
                    "cannot mint installation tokens"
                ),
            }
    except OSError as e:
        return {
            "name": "github_app_private_key_path",
            "config_path": f"secrets.github_app_private_key_path ({e})",
            "impact": (
                "forge_auth=app private key file cannot be read — "
                "cannot mint installation tokens"
            ),
        }
    return None


def get_credential_status(settings: Settings) -> dict[str, Any]:
    """Return the missing-required-credential snapshot for the board.

    ``missing`` lists one entry per absent credential, each with the
    ``config_path`` an operator has to fill in and the ``impact`` of
    leaving it unset.  ``ok`` is ``True`` when the list is empty.

    Only credentials without which mill cannot do its core job are
    reported.  Optional ones (Langfuse, ntfy, ``openrouter_management_key``,
    ``sandbox_push_token``) degrade a feature rather than the pipeline and
    are deliberately left out, so the banner never cries wolf.
    """
    missing = [
        finding
        for finding in (_openrouter_missing(), _forge_missing(settings))
        if finding is not None
    ]
    return {"ok": not missing, "missing": missing}
