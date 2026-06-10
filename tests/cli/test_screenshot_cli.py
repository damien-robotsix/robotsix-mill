"""Tests for ``ticket new --screenshot`` upload wiring in the CLI."""

from __future__ import annotations

import httpx

import robotsix_mill.cli as cli_mod
from robotsix_mill.cli import main


class _Resp:
    def __init__(self, status_code, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = ""

    def json(self):
        return self._json

    def raise_for_status(self):
        if not 200 <= self.status_code < 300:
            raise httpx.HTTPStatusError("", request=None, response=self)


def _make_fake_client(uploads, *, upload_status=201):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kwargs):
            if url == "/tickets":
                return _Resp(201, {"id": "ticket-123"})
            if url.endswith("/screenshots"):
                uploads.append((url, kwargs.get("files")))
                return _Resp(upload_status)
            return _Resp(404, {})

    return FakeClient


def test_ticket_new_uploads_screenshots(tmp_path, capsys, monkeypatch):
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    a.write_bytes(b"aaa")
    b.write_bytes(b"bbb")

    uploads: list = []
    monkeypatch.setattr(cli_mod.httpx, "Client", _make_fake_client(uploads))

    rc = main(
        [
            "ticket",
            "new",
            "--title",
            "T",
            "--repo-id",
            "test-repo",
            "--screenshot",
            str(a),
            "--screenshot",
            str(b),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "ticket-123" in out
    assert len(uploads) == 2
    assert all(u[0] == "/tickets/ticket-123/screenshots" for u in uploads)


def test_ticket_new_upload_failure_still_exits_0(tmp_path, capsys, monkeypatch):
    a = tmp_path / "a.png"
    a.write_bytes(b"aaa")

    uploads: list = []
    monkeypatch.setattr(
        cli_mod.httpx, "Client", _make_fake_client(uploads, upload_status=400)
    )

    rc = main(
        [
            "ticket",
            "new",
            "--title",
            "T",
            "--repo-id",
            "test-repo",
            "--screenshot",
            str(a),
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "ticket-123" in captured.out
    assert "warning" in captured.err.lower()
