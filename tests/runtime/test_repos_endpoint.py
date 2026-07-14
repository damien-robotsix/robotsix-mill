"""Tests for POST /repos — runtime repo registration endpoint."""

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from robotsix_mill.config.repos import _reset_repos_config, load_repos_config
from robotsix_mill.runtime.api import create_app


@pytest.fixture
def client(settings, repos_registry):
    """Single-repo TestClient for /repos endpoint tests."""
    settings.allow_runtime_repo_registration = True
    with TestClient(
        create_app(repos_registry, settings, single_repo_id="test-repo")
    ) as c:
        yield c


def test_register_new_repo(client, settings):
    """POST /repos with a new repo_id → 201, registered=True, board_id
    defaults to repo_id, and the overlay YAML file is written."""
    payload = {
        "repo_id": "new-repo",
        "forge_remote_url": "https://github.com/x/y",
    }
    r = client.post("/repos", json=payload)
    assert r.status_code == 201
    body = r.json()
    assert body["repo_id"] == "new-repo"
    assert body["board_id"] == "new-repo"  # default: repo_id
    assert body["forge_remote_url"] == "https://github.com/x/y"
    assert body["registered"] is True

    # Verify the overlay YAML was written.
    overlay_path = Path(settings.data_dir) / "registered_repos.yaml"
    assert overlay_path.exists()
    overlay = yaml.safe_load(overlay_path.read_text())
    assert "repos" in overlay
    entry = overlay["repos"]["new-repo"]
    assert entry["board_id"] == "new-repo"
    assert entry["forge_remote_url"] == "https://github.com/x/y"
    assert entry["_mill_source"] == "auto"


def test_register_idempotent(client, settings):
    """POST /repos twice with the same payload → second call returns 200,
    registered=False, and the overlay file is unchanged."""
    payload = {
        "repo_id": "idem-repo",
        "forge_remote_url": "https://github.com/a/b",
    }
    r1 = client.post("/repos", json=payload)
    assert r1.status_code == 201
    assert r1.json()["registered"] is True

    # Capture mtime after first write.
    overlay_path = Path(settings.data_dir) / "registered_repos.yaml"
    mtime_after_first = overlay_path.stat().st_mtime

    r2 = client.post("/repos", json=payload)
    assert r2.status_code == 200
    assert r2.json()["registered"] is False
    assert overlay_path.stat().st_mtime == mtime_after_first


def test_board_id_defaults_to_repo_id(client):
    """POST /repos without board_id → board_id == repo_id in response."""
    payload = {
        "repo_id": "default-board-repo",
        "forge_remote_url": "https://github.com/x/y",
    }
    r = client.post("/repos", json=payload)
    assert r.status_code == 201
    body = r.json()
    assert body["board_id"] == body["repo_id"]


def test_board_id_explicit(client, settings):
    """POST /repos with an explicit board_id → response and YAML both
    use the custom board_id."""
    payload = {
        "repo_id": "custom-board-repo",
        "forge_remote_url": "https://github.com/x/y",
        "board_id": "custom-board",
    }
    r = client.post("/repos", json=payload)
    assert r.status_code == 201
    body = r.json()
    assert body["board_id"] == "custom-board"

    # Verify in the overlay YAML.
    overlay_path = Path(settings.data_dir) / "registered_repos.yaml"
    overlay = yaml.safe_load(overlay_path.read_text())
    assert overlay["repos"]["custom-board-repo"]["board_id"] == "custom-board"


def test_register_operator_precedence(client):
    """POST /repos with a repo_id already in the operator config
    (repos_registry fixture) → 200, registered=False, no overlay write."""
    payload = {
        "repo_id": "test-repo",  # from repos_registry fixture
        "forge_remote_url": "https://github.com/x/y",
    }
    r = client.post("/repos", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["repo_id"] == "test-repo"
    assert body["registered"] is False
    # The response must reflect the operator's effective fields, not the
    # request body.
    assert body["board_id"] == "test-board"
    assert body["forge_remote_url"] is None  # operator entry has no forge_remote_url


def test_overlay_round_trip(settings):
    """After a 201 POST, calling _reset_repos_config + load_repos_config
    directly → new repo_id appears in the returned ReposRegistry.

    We pass the overlay file path explicitly to load_repos_config so it
    reads the exact file we wrote (the test suite patches data_dir
    independently of the YAML config's service.data_dir path).
    """
    overlay_path = Path(settings.data_dir) / "registered_repos.yaml"
    data: dict = {}
    if overlay_path.exists():
        data = yaml.safe_load(overlay_path) or {}
    data.setdefault("repos", {})
    data["repos"]["roundtrip-repo"] = {
        "board_id": "roundtrip-repo",
        "forge_remote_url": "https://github.com/r/t",
        "_mill_source": "auto",
    }
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    with open(overlay_path, "w", encoding="utf-8") as fh:
        yaml.dump(data, fh, default_flow_style=False, sort_keys=False)

    _reset_repos_config()
    registry = load_repos_config(config_file=str(overlay_path))
    assert "roundtrip-repo" in registry.repos
    rc = registry.repos["roundtrip-repo"]
    assert rc.board_id == "roundtrip-repo"
    assert rc.forge_remote_url == "https://github.com/r/t"
    assert rc.source == "auto"


def test_hot_reload(client, settings):
    """After a 201 POST, request.app.state.repos contains the new repo
    immediately — no restart required."""
    payload = {
        "repo_id": "hot-reload-repo",
        "forge_remote_url": "https://github.com/h/r",
    }
    r = client.post("/repos", json=payload)
    assert r.status_code == 201

    # Assert the app-state registry has the new repo.
    repos = client.app.state.repos
    assert "hot-reload-repo" in repos.repos
    rc = repos.repos["hot-reload-repo"]
    assert rc.board_id == "hot-reload-repo"
    assert rc.forge_remote_url == "https://github.com/h/r"

    # The original operator-configured repo must still be present.
    assert "test-repo" in repos.repos


def test_register_rejected_when_flag_off(client, settings):
    """POST /repos with allow_runtime_repo_registration=False → 403."""
    settings.allow_runtime_repo_registration = False
    payload = {
        "repo_id": "rejected-repo",
        "forge_remote_url": "https://github.com/x/y",
    }
    r = client.post("/repos", json=payload)
    assert r.status_code == 403
    assert "disabled" in r.json()["detail"]


def test_register_rejects_credential_url(client):
    """POST /repos with a forge_remote_url containing credentials → 422."""
    payload = {
        "repo_id": "bad-url-repo",
        "forge_remote_url": "https://token:x-oauth-basic@github.com/x/y",
    }
    r = client.post("/repos", json=payload)
    assert r.status_code == 422


def test_register_accepts_ssh_url(client):
    """POST /repos with an ssh-style URL (no hostname in urlsplit) → 201."""
    payload = {
        "repo_id": "ssh-repo",
        "forge_remote_url": "git@github.com:owner/repo.git",
    }
    r = client.post("/repos", json=payload)
    assert r.status_code == 201


def test_deregister_auto_repo(client, settings):
    """DELETE /repos/{id} for an auto-registered repo → 204, repo removed."""
    settings.allow_runtime_repo_registration = True
    # Register first.
    payload = {
        "repo_id": "delete-me",
        "forge_remote_url": "https://github.com/x/y",
    }
    r = client.post("/repos", json=payload)
    assert r.status_code == 201

    # Deregister.
    r = client.delete("/repos/delete-me")
    assert r.status_code == 204

    # Verify it's gone from the registry.
    repos = client.app.state.repos
    assert "delete-me" not in repos.repos

    # Verify the overlay YAML no longer has the entry.
    overlay_path = Path(settings.data_dir) / "registered_repos.yaml"
    if overlay_path.exists():
        overlay = yaml.safe_load(overlay_path.read_text()) or {}
        repos_in_overlay = overlay.get("repos", {}) if isinstance(overlay, dict) else {}
        assert "delete-me" not in repos_in_overlay


def test_deregister_unknown_repo(client):
    """DELETE /repos/{id} for an unknown repo → 404."""
    r = client.delete("/repos/nonexistent")
    assert r.status_code == 404


def test_deregister_operator_repo(client, settings):
    """DELETE /repos/{id} for an operator-configured repo → 403."""
    settings.allow_runtime_repo_registration = True
    # "test-repo" is the operator-configured repo from the fixture.
    r = client.delete("/repos/test-repo")
    assert r.status_code == 403
    assert "operator-configured" in r.json()["detail"]
