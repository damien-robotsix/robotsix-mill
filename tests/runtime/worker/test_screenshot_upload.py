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


def test_upload_content_type_fallback_via_guess_type(client, service):
    # Generic content-type but a .png filename → media type is resolved
    # via mimetypes.guess_type and the upload succeeds.
    t = service.create("screenshot ticket")
    r = client.post(
        f"/tickets/{t.id}/screenshots",
        files={"file": ("shot.png", _PNG_BYTES, "application/octet-stream")},
    )
    assert r.status_code == 201, r.text
    assert r.json()["filename"] == "shot.png"
    saved = service.workspace(t).screenshots_dir / "shot.png"
    assert saved.read_bytes() == _PNG_BYTES


def test_upload_auto_generates_filename(client, service):
    # Blank/dot filenames → server auto-generates screenshot-{n}{ext},
    # with the counter driven by the current screenshot count.
    t = service.create("screenshot ticket")
    r1 = client.post(
        f"/tickets/{t.id}/screenshots",
        files={"file": (".", _PNG_BYTES, "image/png")},
    )
    assert r1.status_code == 201, r1.text
    assert r1.json()["filename"] == "screenshot-1.png"

    r2 = client.post(
        f"/tickets/{t.id}/screenshots",
        files={"file": ("..", _PNG_BYTES, "image/png")},
    )
    assert r2.status_code == 201, r2.text
    assert r2.json()["filename"] == "screenshot-2.png"

    ssdir = service.workspace(t).screenshots_dir
    assert sorted(p.name for p in ssdir.iterdir()) == [
        "screenshot-1.png",
        "screenshot-2.png",
    ]


def test_upload_non_png_formats_accepted(client, service):
    # Validation is content-type driven, so the bytes need not be real
    # images for jpeg/gif/webp.
    t = service.create("screenshot ticket")
    ssdir = service.workspace(t).screenshots_dir
    for name, ctype in (
        ("shot.jpg", "image/jpeg"),
        ("shot.gif", "image/gif"),
        ("shot.webp", "image/webp"),
    ):
        blob = b"blob-" + ctype.encode()
        r = client.post(
            f"/tickets/{t.id}/screenshots",
            files={"file": (name, blob, ctype)},
        )
        assert r.status_code == 201, r.text
        assert (ssdir / name).read_bytes() == blob


def test_upload_duplicate_basename_overwrites(client, service):
    # Same basename twice → last write wins, only one file present.
    t = service.create("screenshot ticket")
    client.post(
        f"/tickets/{t.id}/screenshots",
        files={"file": ("dup.png", b"first", "image/png")},
    )
    r = client.post(
        f"/tickets/{t.id}/screenshots",
        files={"file": ("dup.png", b"second", "image/png")},
    )
    assert r.status_code == 201, r.text
    ssdir = service.workspace(t).screenshots_dir
    assert [p.name for p in ssdir.iterdir()] == ["dup.png"]
    assert (ssdir / "dup.png").read_bytes() == b"second"


def test_upload_over_size_limit_returns_413(client, service, monkeypatch):
    # Shrink the limit and upload more bytes than it → 413, nothing written.
    from robotsix_mill.runtime.routes import _tickets

    monkeypatch.setattr(_tickets, "_MAX_SCREENSHOT_BYTES", 10)
    t = service.create("screenshot ticket")
    r = client.post(
        f"/tickets/{t.id}/screenshots",
        files={"file": ("big.png", b"x" * 50, "image/png")},
    )
    assert r.status_code == 413, r.text
    ssdir = service.workspace(t).screenshots_dir
    assert not ssdir.exists() or list(ssdir.iterdir()) == []


def test_upload_write_failure_returns_500(client, service, monkeypatch):
    # An OSError while persisting the bytes → 500 with a clean detail the
    # frontend can surface, not an unstyled generic error.
    from pathlib import Path

    def _boom(self, data):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_bytes", _boom)
    t = service.create("screenshot ticket")
    r = client.post(
        f"/tickets/{t.id}/screenshots",
        files={"file": ("shot.png", _PNG_BYTES, "image/png")},
    )
    assert r.status_code == 500, r.text
    assert r.json()["detail"] == "failed to save screenshot"
