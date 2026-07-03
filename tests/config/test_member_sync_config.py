"""Settings + YAML + env-var resolution for the member-sync pass.

member-sync is a deterministic pass (no model, no memory ledger): only
the ``enabled`` flag and ``interval_seconds`` are configurable.
"""

from __future__ import annotations

import json
import pathlib

from robotsix_mill.config import Settings


def test_defaults():
    s = Settings()
    assert s.member_sync_periodic is True
    assert s.member_sync_interval_seconds == 86400


def test_env_var_overrides(monkeypatch):
    monkeypatch.setenv("MILL_MEMBER_SYNC_PERIODIC", "0")
    monkeypatch.setenv("MILL_MEMBER_SYNC_INTERVAL_SECONDS", "3600")
    s = Settings()
    assert s.member_sync_periodic is False
    assert s.member_sync_interval_seconds == 3600


def test_yaml_mapping_resolves():
    config_path = pathlib.Path("config/config.example.json")
    raw = json.loads(config_path.read_text())
    settings = raw["settings"]

    assert settings["member_sync_periodic"] is True
    assert settings["member_sync_interval_seconds"] == 86400

    s = Settings(**settings)
    assert s.member_sync_periodic is True
    assert s.member_sync_interval_seconds == 86400
