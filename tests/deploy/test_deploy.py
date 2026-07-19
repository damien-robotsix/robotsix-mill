"""Unit tests for ``robotsix_mill.deploy`` — deploy-freshness checks."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import httpx
import pytest

from robotsix_mill.deploy import DeployStatus, check_deploy_freshness


# ---------------------------------------------------------------------------
# Fake httpx client helpers
# ---------------------------------------------------------------------------


class _FakeResponse:
    """A minimal fake httpx.Response for testing check_deploy_freshness."""

    def __init__(self, status_code: int, json_data: dict | None = None):
        self.status_code = status_code
        self._json = json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)  # type: ignore[arg-type]

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _FakeClient:
    """A fake httpx.Client that returns a canned response for GET."""

    def __init__(self, response, **kw):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def get(self, url):
        return self._response


# ---------------------------------------------------------------------------
# check_deploy_freshness
# ---------------------------------------------------------------------------


def test_check_deploy_freshness_none_url_returns_none():
    """When deploy_api_url is None, the gate is disabled."""
    assert check_deploy_freshness(None) is None


def test_check_deploy_freshness_empty_url_returns_none():
    """When deploy_api_url is empty string, the gate is disabled."""
    assert check_deploy_freshness("") is None


def test_check_deploy_freshness_current_image(monkeypatch):
    """When running_digest == latest_digest, update_available is False."""
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: _FakeClient(
            _FakeResponse(
                200,
                {
                    "running_digest": "sha256:abc123",
                    "latest_digest": "sha256:abc123",
                    "update_available": False,
                },
            )
        ),
    )
    status = check_deploy_freshness("http://deploy:8080")
    assert status is not None
    assert status.running_digest == "sha256:abc123"
    assert status.latest_digest == "sha256:abc123"
    assert status.update_available is False


def test_check_deploy_freshness_stale_image(monkeypatch):
    """When running_digest != latest_digest, update_available is True."""
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: _FakeClient(
            _FakeResponse(
                200,
                {
                    "running_digest": "sha256:old",
                    "latest_digest": "sha256:new",
                    "update_available": True,
                },
            )
        ),
    )
    status = check_deploy_freshness("http://deploy:8080")
    assert status is not None
    assert status.running_digest == "sha256:old"
    assert status.latest_digest == "sha256:new"
    assert status.update_available is True


def test_check_deploy_freshness_update_available_inferred(monkeypatch):
    """When update_available key is missing, it's inferred from digest mismatch."""
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: _FakeClient(
            _FakeResponse(
                200,
                {
                    "running_digest": "sha256:old",
                    "latest_digest": "sha256:new",
                },
            )
        ),
    )
    status = check_deploy_freshness("http://deploy:8080")
    assert status is not None
    assert status.update_available is True


def test_check_deploy_freshness_server_unreachable_returns_none(monkeypatch):
    """When the deploy server returns 500, return None (don't block)."""
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: _FakeClient(_FakeResponse(500)),
    )
    status = check_deploy_freshness("http://deploy:8080")
    assert status is None


def test_check_deploy_freshness_unexpected_payload_returns_none(monkeypatch):
    """When the response is missing required keys, return None."""
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: _FakeClient(_FakeResponse(200, {"unexpected": "payload"})),
    )
    status = check_deploy_freshness("http://deploy:8080")
    assert status is None


def test_check_deploy_freshness_connection_error_returns_none(monkeypatch):
    """When the deploy server cannot be reached, return None."""

    class _ErrorClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "Client", _ErrorClient)
    status = check_deploy_freshness("http://deploy:8080")
    assert status is None


# ---------------------------------------------------------------------------
# DeployStatus dataclass
# ---------------------------------------------------------------------------


def test_deploy_status_immutable():
    """DeployStatus is frozen."""
    status = DeployStatus(
        running_digest="sha256:a", latest_digest="sha256:b", update_available=True
    )
    with pytest.raises(FrozenInstanceError):
        status.running_digest = "changed"  # type: ignore[misc]


def test_deploy_status_equality():
    """DeployStatus supports equality comparison."""
    a = DeployStatus(running_digest="a", latest_digest="b", update_available=True)
    b = DeployStatus(running_digest="a", latest_digest="b", update_available=True)
    c = DeployStatus(running_digest="a", latest_digest="a", update_available=False)
    assert a == b
    assert a != c
