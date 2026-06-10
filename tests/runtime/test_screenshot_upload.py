"""Tests for the ``POST /tickets/{id}/screenshots`` upload endpoint."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from robotsix_mill.runtime.api import create_app

# Minimal valid 1x1 PNG.
_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture
def client(settings, repos_registry):
    with TestClient(
        create_app(repos_registry, settings, single_repo_id="test-repo")
    ) as c:
        yield c


def test_upload_png_saves_and_returns_201(client, service):
    t = service.create("screenshot ticket")
    r = client.post(
        f"/tickets/{t.id}/screenshots",
        files={"file": ("shot.png", _PNG_BYTES, "image/png")},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body == {"filename": "shot.png", "ticket_id": t.id}

    saved = service.workspace(t).screenshots_dir / "shot.png"
    assert saved.read_bytes() == _PNG_BYTES


def test_upload_to_nonexistent_ticket_returns_404(client):
    r = client.post(
        "/tickets/does-not-exist/screenshots",
        files={"file": ("shot.png", _PNG_BYTES, "image/png")},
    )
    assert r.status_code == 404


def test_upload_non_image_returns_400(client, service):
    t = service.create("screenshot ticket")
    r = client.post(
        f"/tickets/{t.id}/screenshots",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert r.status_code == 400


def test_upload_strips_path_traversal(client, service):
    t = service.create("screenshot ticket")
    r = client.post(
        f"/tickets/{t.id}/screenshots",
        files={"file": ("../../etc/evil.png", _PNG_BYTES, "image/png")},
    )
    assert r.status_code == 201, r.text
    assert r.json()["filename"] == "evil.png"
    ssdir = service.workspace(t).screenshots_dir
    assert (ssdir / "evil.png").exists()
    # Nothing escaped the screenshots dir.
    assert [p.name for p in ssdir.iterdir()] == ["evil.png"]
