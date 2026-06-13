"""Settings + YAML + env-var resolution for the member-sync pass.

member-sync is a deterministic pass (no model, no memory ledger): only
the ``enabled`` flag and ``interval_seconds`` are configurable.
"""

from __future__ import annotations

from robotsix_mill.config import Settings
from robotsix_mill.config.loader import flatten_yaml_config


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
    flat = flatten_yaml_config(
        {
            "periodic": {
                "member_sync": {
                    "enabled": False,
                    "interval_seconds": 1234,
                }
            }
        }
    )
    assert flat["member_sync_periodic"] is False
    assert flat["member_sync_interval_seconds"] == 1234
    s = Settings(**flat)
    assert s.member_sync_periodic is False
    assert s.member_sync_interval_seconds == 1234
