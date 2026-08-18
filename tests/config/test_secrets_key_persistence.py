"""The secrets key must survive container recreation.

``_derive_fernet_key`` keys off ``os.uname().nodename``, which in a
container is the container ID.  That is stable across a restart but not
across a recreate, and every image update recreates — so an update used to
leave ``config.json`` intact but undecryptable, bringing mill up with no
OpenRouter key and no GitHub App while every ticket blocked.
"""

from __future__ import annotations

import json
import stat

import pytest

from robotsix_mill.config import loader

SECRETS = {"openrouter_api_key": "sk-test", "github_app_id": "12345"}


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """Point the loader at an isolated config file."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"secrets": {}}), encoding="utf-8")
    monkeypatch.setenv("ROBOTSIX_CONFIG_FILE", str(path))
    return path


def _write_block(path, token):
    data = json.loads(path.read_text(encoding="utf-8"))
    data["secrets"] = token
    path.write_text(json.dumps(data), encoding="utf-8")


def test_round_trip_uses_persisted_key(config_dir):
    """Encrypt then decrypt through the persisted key file."""
    token = loader.encrypt_secrets_block(SECRETS)
    assert loader._key_file_path().exists()
    assert loader.decrypt_secrets_block(token) == SECRETS


def test_key_file_is_owner_only(config_dir):
    """The key must never be group- or world-readable."""
    loader.encrypt_secrets_block(SECRETS)
    mode = loader._key_file_path().stat().st_mode
    assert not mode & (stat.S_IRWXG | stat.S_IRWXO)


def test_survives_hostname_change(config_dir, monkeypatch):
    """A recreated container gets a new hostname — secrets must still read."""
    token = loader.encrypt_secrets_block(SECRETS)
    _write_block(config_dir, token)

    real_uname = loader.os.uname()
    monkeypatch.setattr(
        loader.os,
        "uname",
        lambda: type(real_uname)(
            (
                real_uname.sysname,
                "a-brand-new-container-id",
                real_uname.release,
                real_uname.version,
                real_uname.machine,
            )
        ),
    )
    assert loader.load_secrets_block() == SECRETS


def test_legacy_hostname_block_is_migrated(config_dir):
    """A block written under the old hostname key is re-encrypted on load."""
    from cryptography.fernet import Fernet

    legacy = Fernet(loader._derive_fernet_key()).encrypt(json.dumps(SECRETS).encode())
    _write_block(config_dir, legacy.decode())
    key_path = loader._key_file_path()
    assert not key_path.exists()

    assert loader.load_secrets_block() == SECRETS

    # Migration happened: a key now exists and the stored token changed.
    assert key_path.exists()
    stored = json.loads(config_dir.read_text(encoding="utf-8"))["secrets"]
    assert stored != legacy.decode()
    # And the migrated block reads back without the legacy key.
    assert loader._decrypt_with(stored, key_path.read_bytes().strip()) == SECRETS


def test_migration_leaves_config_intact_on_failure(config_dir, monkeypatch):
    """A failed migration must not damage the existing config."""
    from cryptography.fernet import Fernet

    legacy = Fernet(loader._derive_fernet_key()).encrypt(json.dumps(SECRETS).encode())
    _write_block(config_dir, legacy.decode())
    before = config_dir.read_text(encoding="utf-8")

    monkeypatch.setattr(loader, "_persistent_fernet_key", lambda *, create: None)
    assert loader.load_secrets_block() == SECRETS
    assert config_dir.read_text(encoding="utf-8") == before
