"""Tests for the config-ownership HTTP surface (GET/PUT /config,
GET /config/versions, POST /config/rollback).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from robotsix_mill.runtime.api import create_app


@pytest.fixture
def tmp_config_file(tmp_path, monkeypatch):
    """Create a temporary config.json and point ROBOTSIX_CONFIG_FILE at it."""
    config_path = tmp_path / "config.json"
    config_data = {
        "settings": {
            "api_port": 8077,
            "data_dir": str(tmp_path / "data"),
            "auto_approve_enabled": False,
            "review_enabled": False,
        },
        "secrets": {
            "openrouter_api_key": "SECRET",
            "forge_token": "SECRET",
        },
        "repos": {},
    }
    config_path.write_text(json.dumps(config_data, indent=2))
    monkeypatch.setenv("ROBOTSIX_CONFIG_FILE", str(config_path))
    # Ensure data dir exists for version history
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return config_path


@pytest.fixture
def config_client(settings, repos_registry, tmp_config_file, monkeypatch):
    """TestClient with a temporary config file for testing config endpoints."""
    # Create the data_dir referenced in the config
    data_dir = Path(str(tmp_config_file.parent / "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    # Override settings.data_dir to use our temp path
    monkeypatch.setattr(settings, "data_dir", data_dir)
    with TestClient(
        create_app(repos_registry, settings, single_repo_id="test-repo")
    ) as c:
        yield c


class TestGetConfig:
    def test_returns_200_with_correct_shape(self, config_client):
        """GET /config returns 200 with config, schema, and version keys."""
        r = config_client.get("/config")
        assert r.status_code == 200
        data = r.json()
        assert "config" in data
        assert "schema" in data
        assert "version" in data
        assert isinstance(data["config"], dict)
        assert isinstance(data["schema"], dict)
        assert isinstance(data["version"], int)

    def test_includes_settings_fields(self, config_client):
        """GET /config returns the settings from the config file."""
        r = config_client.get("/config")
        assert r.status_code == 200
        data = r.json()
        assert data["config"]["api_port"] == 8077
        assert data["config"]["auto_approve_enabled"] is False

    def test_masks_secret_fields(self, config_client):
        """GET /config masks secret values as '**********'."""
        r = config_client.get("/config")
        assert r.status_code == 200
        data = r.json()
        # Secret fields from Secrets model should be masked
        if "openrouter_api_key" in data["config"]:
            assert data["config"]["openrouter_api_key"] == "**********"
        if "forge_token" in data["config"]:
            assert data["config"]["forge_token"] == "**********"

    def test_schema_has_properties(self, config_client):
        """GET /config schema has a properties dict."""
        r = config_client.get("/config")
        assert r.status_code == 200
        data = r.json()
        assert "properties" in data["schema"]
        assert isinstance(data["schema"]["properties"], dict)


class TestPutConfig:
    def test_updates_non_secret_field(self, config_client, tmp_config_file):
        """PUT /config updates a non-secret field and returns new config."""
        r = config_client.put("/config", json={"auto_approve_enabled": True})
        assert r.status_code == 200
        data = r.json()
        assert data["config"]["auto_approve_enabled"] is True
        assert data["version"] > 0

        # Verify the file was updated
        raw = json.loads(tmp_config_file.read_text())
        assert raw["settings"]["auto_approve_enabled"] is True

    def test_secret_merge_on_write_blank_keeps_existing(
        self, config_client, tmp_config_file
    ):
        """PUT /config with a blank or masked secret keeps the stored value."""
        # Write a real secret value first
        raw = json.loads(tmp_config_file.read_text())
        raw["secrets"]["openrouter_api_key"] = "sk-real-secret"
        tmp_config_file.write_text(json.dumps(raw, indent=2))

        # Submit a masked value — should keep existing
        r = config_client.put("/config", json={"openrouter_api_key": "**********"})
        assert r.status_code == 200

        raw = json.loads(tmp_config_file.read_text())
        # Secrets block is base64-encoded at rest.
        decoded = base64.b64decode(raw["secrets"]).decode()
        assert json.loads(decoded)["openrouter_api_key"] == "sk-real-secret"

    def test_secret_merge_on_write_explicit_overwrites(
        self, config_client, tmp_config_file
    ):
        """PUT /config with an explicit secret value overwrites the stored value."""
        # Write a real secret value first
        raw = json.loads(tmp_config_file.read_text())
        raw["secrets"]["openrouter_api_key"] = "sk-old-secret"
        tmp_config_file.write_text(json.dumps(raw, indent=2))

        # Submit an explicit value — should overwrite
        r = config_client.put("/config", json={"openrouter_api_key": "sk-new-secret"})
        assert r.status_code == 200

        raw = json.loads(tmp_config_file.read_text())
        # Secrets block is base64-encoded at rest.
        decoded = base64.b64decode(raw["secrets"]).decode()
        assert json.loads(decoded)["openrouter_api_key"] == "sk-new-secret"

    def test_secret_written_to_secrets_block(self, config_client, tmp_config_file):
        """PUT /config writes secret values to the secrets block."""
        r = config_client.put("/config", json={"openrouter_api_key": "sk-test"})
        assert r.status_code == 200

        raw = json.loads(tmp_config_file.read_text())
        assert "secrets" in raw
        # Secrets block is base64-encoded at rest.
        decoded = base64.b64decode(raw["secrets"]).decode()
        assert json.loads(decoded)["openrouter_api_key"] == "sk-test"
        # Should NOT be in settings
        assert "openrouter_api_key" not in raw.get("settings", {})

    def test_rejects_unknown_key(self, config_client):
        """PUT /config rejects keys not in the Settings model."""
        r = config_client.put("/config", json={"nonexistent_key": "value"})
        assert r.status_code == 422
        data = r.json()
        assert data["type"] == "urn:robotsix:error:config-validation"

    def test_validates_field_type(self, config_client):
        """PUT /config validates the type of updated fields."""
        r = config_client.put("/config", json={"api_port": "not-a-number"})
        assert r.status_code == 422
        data = r.json()
        assert data["type"] == "urn:robotsix:error:config-validation"

    def test_partial_update_preserves_other_keys(self, config_client, tmp_config_file):
        """PUT /config only changes the submitted keys."""
        r = config_client.put("/config", json={"auto_approve_enabled": True})
        assert r.status_code == 200

        raw = json.loads(tmp_config_file.read_text())
        assert raw["settings"]["api_port"] == 8077  # unchanged
        assert raw["settings"]["auto_approve_enabled"] is True  # changed
        assert raw["settings"]["review_enabled"] is False  # unchanged

    def test_update_with_langfuse_block(self, tmp_path, monkeypatch):
        """PUT /config succeeds when the config file has a langfuse block.

        Regression test: the config snapshot stored in version history
        must be JSON-serializable even when Settings fields contain
        Pydantic model instances (e.g. LangfuseConfig).
        """
        config_path = tmp_path / "config.json"
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        config_data = {
            "settings": {
                "api_port": 8077,
                "data_dir": str(data_dir),
                "trace_review_interval_seconds": 86400,
            },
            "langfuse": {
                "host": "https://langfuse.example.com",
                "projects": {
                    "mill": {
                        "public_key": "pk-test",
                        "secret_key": "sk-test",
                        "project_id": "test-project",
                    }
                },
            },
        }
        config_path.write_text(json.dumps(config_data, indent=2))
        monkeypatch.setenv("ROBOTSIX_CONFIG_FILE", str(config_path))

        from robotsix_mill.runtime.config_service import update_config

        result = update_config(
            {"trace_review_interval_seconds": 1209600}, data_dir=data_dir
        )
        assert result["version"] >= 1
        assert result["config"]["trace_review_interval_seconds"] == 1209600
        # langfuse must be a plain dict, not a Pydantic model instance
        assert isinstance(result["config"]["langfuse"], dict)
        assert result["config"]["langfuse"]["host"] == "https://langfuse.example.com"


class TestGetConfigVersions:
    def test_returns_empty_versions_initially(self, config_client):
        """GET /config/versions returns empty list when no writes have occurred."""
        r = config_client.get("/config/versions")
        assert r.status_code == 200
        data = r.json()
        assert "versions" in data
        assert isinstance(data["versions"], list)

    def test_returns_versions_after_update(self, config_client):
        """GET /config/versions lists versions after a config update."""
        # Make an update to create a version
        config_client.put("/config", json={"auto_approve_enabled": True})
        config_client.put("/config", json={"review_enabled": True})

        r = config_client.get("/config/versions")
        assert r.status_code == 200
        data = r.json()
        assert len(data["versions"]) >= 2

        # Each version has the expected keys
        for v in data["versions"]:
            assert "version" in v
            assert "timestamp" in v
            assert "changed_keys" in v


class TestPostConfigRollback:
    def test_rollback_creates_new_version(self, config_client):
        """POST /config/rollback restores a previous version and creates a new one."""
        # Make an initial update
        r1 = config_client.put("/config", json={"auto_approve_enabled": True})
        v1 = r1.json()["version"]

        # Make another update
        r2 = config_client.put("/config", json={"auto_approve_enabled": False})
        v2 = r2.json()["version"]
        assert v2 > v1

        # Rollback to v1
        r3 = config_client.post("/config/rollback", json={"version": v1})
        assert r3.status_code == 200
        data = r3.json()
        assert data["version"] > v2  # rollback creates a new version
        assert data["config"]["auto_approve_enabled"] is True

    def test_rollback_invalid_version(self, config_client):
        """POST /config/rollback with nonexistent version returns 422."""
        r = config_client.post("/config/rollback", json={"version": 9999})
        assert r.status_code == 422
        data = r.json()
        assert data["type"] == "urn:robotsix:error:config-validation"

    def test_rollback_missing_version(self, config_client):
        """POST /config/rollback without version returns 422."""
        r = config_client.post("/config/rollback", json={})
        assert r.status_code == 422
        data = r.json()
        assert data["type"] == "urn:robotsix:error:config-validation"
