"""Config loader — all loading is now via robotsix_config."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import yaml
from cryptography.fernet import Fernet


class ConfigError(Exception):
    """Raised for config-loading failures."""


def _resolve_main_config_path() -> Path | None:
    """Resolve the main config file path.

    Checks ``ROBOTSIX_CONFIG_FILE`` (used by robotsix_config and tests),
    then the default ``config/config.json``.
    """
    env_path = os.environ.get("ROBOTSIX_CONFIG_FILE")
    if env_path:
        return Path(env_path)
    default = Path("config/config.json")
    if default.exists():
        return default
    return None


def _load_file(target: Path) -> dict[str, Any]:
    """Load a YAML or JSON file, returning a dict (or {} on error)."""
    try:
        raw_text = target.read_text(encoding="utf-8")
    except FileNotFoundError, OSError:
        return {}

    # Try JSON first, then YAML.
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        try:
            data = yaml.safe_load(raw_text)
        except yaml.YAMLError:
            return {}

    return data if isinstance(data, dict) else {}


def load_settings_block() -> dict[str, Any]:
    """Return the main config file's ``settings`` block, alias-keyed.

    Backs :class:`~robotsix_mill.config.json_source.JsonSettingsSource`, so
    ``Settings()`` picks the operator's config up again. The clean-cutover to
    the config-standard (#2525) dropped that source in favour of
    ``load_settings()`` but never rewired the call sites, and nothing else
    reads the file — so every one of the several hundred bare ``Settings()``
    constructions silently fell back to model defaults. Undeployed, that
    would have reverted mill's entire configuration on the next deploy.

    Falls back to the top level for flat files (no ``settings`` block), and
    strips the sibling blocks that belong to other models. Never raises: a
    missing or malformed file means "all defaults", matching
    ``robotsix_config.load_config`` semantics.
    """
    main_path = _resolve_main_config_path()
    if main_path is None:
        return {}
    data = _load_file(main_path)
    block = data.get("settings")
    if isinstance(block, dict):
        block = dict(block)
    else:
        # Flat file: everything except the sibling blocks is a setting.
        block = {
            k: v
            for k, v in data.items()
            if k not in ("secrets", "repos", "core", "langfuse", "openrouter")
        }
    # Lift the top-level langfuse block into settings so
    # Settings.langfuse is populated from the canonical top-level key
    # (robotsix-standards#189).
    if "langfuse" in data and isinstance(data["langfuse"], dict):
        block["langfuse"] = data["langfuse"]
    # Lift the top-level openrouter block into settings so
    # Settings.openrouter is populated from the canonical top-level key
    # (robotsix-standards component standard).
    if "openrouter" in data and isinstance(data["openrouter"], dict):
        block["openrouter"] = data["openrouter"]
    # Lift openrouter_api_key from the secrets block (if present and
    # decrypted) into settings so the _migrate_openrouter_api_key model
    # validator populates the canonical openrouter.keys map.  This handles
    # Fernet-encrypted deployed configs where the key lives only in the
    # secrets block, not in settings.
    if "openrouter_api_key" not in block:
        secrets_block = load_secrets_block()
        flat_key = secrets_block.get("openrouter_api_key")
        if flat_key:
            block["openrouter_api_key"] = flat_key
    return block


#: Key file kept beside the main config, on the same volume.
_KEY_FILENAME = ".secrets.key"


def _derive_fernet_key() -> bytes:
    """Derive a Fernet key from the machine hostname — **legacy only**.

    Kept solely so a config encrypted before :func:`_persistent_fernet_key`
    existed can still be read once and migrated.  Never use it to encrypt.

    In a container ``os.uname().nodename`` is the container ID, so this is
    stable across a *restart* but not across a *recreate* — and every image
    update recreates.  That silently orphaned the secrets block on update:
    the file was intact and readable, but undecryptable, so mill came up
    with no OpenRouter key and no GitHub App and every ticket blocked.
    """
    hostname = os.uname().nodename
    raw = hashlib.sha256(hostname.encode() + b":robotsix-mill-config-v1").digest()
    return base64.urlsafe_b64encode(raw)


def _key_file_path() -> Path | None:
    """Path of the persisted key file, or ``None`` if config has no home."""
    main_path = _resolve_main_config_path()
    return None if main_path is None else main_path.parent / _KEY_FILENAME


def _persistent_fernet_key(*, create: bool) -> bytes | None:
    """Return the key stored beside the config, creating it when asked.

    Lives on the same volume as ``config.json``, so it survives container
    recreation.  Returns ``None`` when there is no config path or the
    directory is not writable — callers then fall back to the legacy
    hostname key so read-only and test environments keep working.
    """
    path = _key_file_path()
    if path is None:
        return None
    try:
        if path.exists():
            key = path.read_bytes().strip()
            if key:
                return key
    except OSError:
        return None
    if not create:
        return None
    key = Fernet.generate_key()
    try:
        # Create 0600 from the start — never briefly world-readable.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
    except FileExistsError:
        try:
            return path.read_bytes().strip() or None
        except OSError:
            return None
    except OSError:
        return None
    return key


def encrypt_secrets_block(secrets: dict[str, Any]) -> str:
    """Encrypt a secrets dict for at-rest storage in config.json.

    Returns a Fernet token (base64 string).
    """
    key = _persistent_fernet_key(create=True) or _derive_fernet_key()
    f = Fernet(key)
    return f.encrypt(json.dumps(secrets).encode()).decode()


def _decrypt_with(token: str, key: bytes) -> dict[str, Any] | None:
    try:
        loaded = json.loads(Fernet(key).decrypt(token.encode()))
    except Exception:
        # Decryption failure (wrong key, corruption) — treat block as unset.
        return None
    return loaded if isinstance(loaded, dict) else None


def decrypt_secrets_block(token: str) -> dict[str, Any] | None:
    """Decrypt a Fernet token back to a secrets dict.

    Tries the persisted key first, then the legacy hostname-derived key.
    Returns ``None`` on any failure (wrong key, corruption, etc.).
    """
    key = _persistent_fernet_key(create=False)
    if key is not None:
        decrypted = _decrypt_with(token, key)
        if decrypted is not None:
            return decrypted
    return _decrypt_with(token, _derive_fernet_key())


def _migrate_secrets_to_persistent_key(secrets: dict[str, Any]) -> None:
    """Re-encrypt *secrets* under the persisted key and rewrite the config.

    Called when a block only decrypted under the legacy hostname key.  The
    migration has to happen while that hostname is still current: once the
    container is recreated the old key is gone for good.  Best-effort and
    silent — a failure here leaves the config exactly as it was.
    """
    main_path = _resolve_main_config_path()
    if main_path is None or _persistent_fernet_key(create=False) is not None:
        return
    if _persistent_fernet_key(create=True) is None:
        return
    try:
        data = _load_file(main_path)
        if not data:
            return
        data["secrets"] = encrypt_secrets_block(secrets)
        tmp = main_path.with_name(main_path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(data, indent=2).encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, main_path)
    except OSError:
        return


def load_secrets_block() -> dict[str, Any]:
    """Return the main config file's ``secrets`` block.

    The sibling of :func:`load_settings_block`, for
    :class:`~robotsix_mill.config.secrets.Secrets`. The clean-cutover
    (#2525) rewrote ``Secrets`` as a plain accessor documented as "backed
    by the live ``Settings``", but its ``__init__`` reads only explicit
    kwargs and an explicit ``_secrets_file`` — nothing ever pointed it at
    the config file, so every credential (OpenRouter key, GitHub App id +
    private key, forge tokens, Langfuse keys, ntfy) read back as ``None``
    and mill could neither call an LLM nor authenticate to GitHub.

    Never raises: a missing or malformed file means "all unset", matching
    :func:`load_settings_block`.

    The ``secrets`` block may be stored base64-encoded (new format) or as
    a plain dict (legacy format).  Both are handled transparently.
    """
    main_path = _resolve_main_config_path()
    if main_path is None:
        return {}
    block = _load_file(main_path).get("secrets")
    if isinstance(block, str) and block:
        # New format: Fernet-encrypted JSON string.
        decrypted = decrypt_secrets_block(block)
        if decrypted is not None:
            _migrate_secrets_to_persistent_key(decrypted)
            return decrypted
        # Legacy fallback: try base64 (previous format) or plain dict.
        try:
            decoded = base64.b64decode(block)
            loaded = json.loads(decoded)
            if isinstance(loaded, dict):
                return loaded
        except Exception:  # noqa: S110 — Decoding failure; treat block as unset.
            pass
        return {}
    return dict(block) if isinstance(block, dict) else {}


def _resolve_data_dir() -> Path:
    """Resolve ``data_dir`` from the main config when available, else ``.data``."""
    main_path = _resolve_main_config_path()
    if main_path is not None:
        main_data = _load_file(main_path)
        settings_block = main_data.get("settings", {})
        if isinstance(settings_block, dict):
            dd = settings_block.get("data_dir")
            if isinstance(dd, str) and dd:
                return Path(dd)
    return Path(".data")


def load_repos_yaml(file_path: str | None = None) -> dict[str, object]:
    """Load the ``repos:`` block.

    Priority:
    1. If *file_path* is given, read that file (YAML or JSON).
    2. Otherwise, read from the main ``config/config.json`` and merge in the
       machine-owned overlay (``<data_dir>/registered_repos.yaml``).

    Returns the raw ``repos`` mapping (or ``{}`` on any error / missing file).
    """
    # 1. Explicit file_path parameter wins.
    if file_path is not None:
        data = _load_file(Path(file_path))
        return data.get("repos", {}) if isinstance(data.get("repos"), dict) else {}

    # 2. Default: load main config + overlay merge.
    main_path = _resolve_main_config_path()
    main_repos: dict[str, Any] = {}
    if main_path is not None:
        main_data = _load_file(main_path)
        main_repos = (
            main_data.get("repos", {})
            if isinstance(main_data.get("repos"), dict)
            else {}
        )

    # Merge overlay (machine-owned auto-registered repos).
    overlay_path = _resolve_data_dir() / "registered_repos.yaml"
    overlay_data = _load_file(overlay_path)
    overlay_repos: dict[str, Any] = (
        overlay_data.get("repos", {})
        if isinstance(overlay_data.get("repos"), dict)
        else {}
    )

    # Merge: overlay first, then operator (operator wins on conflict).
    merged = {**overlay_repos, **main_repos}
    return merged
