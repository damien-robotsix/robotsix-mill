"""Tests for the GitHubForge security-feature mixin.

Monkeypatches ``forge._http.put`` directly so the tests exercise
``enable_vulnerability_alerts``, ``enable_automated_security_fixes``,
and ``ensure_dependency_graph_enabled`` without a real HTTP transport.
"""

import httpx as real_httpx

from robotsix_mill.config import Secrets, Settings, _reset_secrets
from robotsix_mill.forge.github import GitHubForge


def _set_secrets(**kw):
    import robotsix_mill.config as _cfg

    _reset_secrets()
    _cfg._secrets = Secrets(**kw)


def _forge(tmp_path, **kw):
    kw.setdefault("data_dir", str(tmp_path))
    kw.setdefault("FORGE_KIND", "github")
    kw.setdefault("FORGE_REMOTE_URL", "https://github.com/o/r.git")
    kw.setdefault("FORGE_TOKEN", "tok")
    s = Settings(**kw)
    _set_secrets(forge_token="tok")
    return GitHubForge(s)


def _resp(status_code):
    return type(
        "FakeResponse",
        (),
        {
            "status_code": status_code,
            "text": "",
            "json": lambda self: {},
            "raise_for_status": lambda self: (
                None
                if 200 <= self.status_code < 300
                else (_ for _ in ()).throw(
                    real_httpx.HTTPStatusError(
                        f"HTTP {self.status_code}",
                        request=real_httpx.Request("PUT", "http://x"),
                        response=self,
                    )
                )
            ),
        },
    )()


# --- enable_vulnerability_alerts ---


def test_enable_vulnerability_alerts_success(tmp_path, monkeypatch):
    """204 No Content → True."""
    forge = _forge(tmp_path)

    def fake_put(path):
        assert "/vulnerability-alerts" in path
        return _resp(204)

    monkeypatch.setattr(forge._http, "put", fake_put)
    assert forge.enable_vulnerability_alerts() is True


def test_enable_vulnerability_alerts_200_also_success(tmp_path, monkeypatch):
    """200 OK → True (defensive — GitHub docs say 204 but accept any 2xx)."""
    forge = _forge(tmp_path)

    def fake_put(path):
        return _resp(200)

    monkeypatch.setattr(forge._http, "put", fake_put)
    assert forge.enable_vulnerability_alerts() is True


def test_enable_vulnerability_alerts_403_returns_false(tmp_path, monkeypatch):
    """403 (token lacks permission) → False."""
    forge = _forge(tmp_path)
    monkeypatch.setattr(forge._http, "put", lambda path: _resp(403))
    assert forge.enable_vulnerability_alerts() is False


def test_enable_vulnerability_alerts_404_returns_false(tmp_path, monkeypatch):
    """404 (repo not found) → False."""
    forge = _forge(tmp_path)
    monkeypatch.setattr(forge._http, "put", lambda path: _resp(404))
    assert forge.enable_vulnerability_alerts() is False


def test_enable_vulnerability_alerts_transport_error_returns_false(
    tmp_path, monkeypatch
):
    """Transport error → False — never raises."""
    forge = _forge(tmp_path)

    def boom(path):
        raise real_httpx.ConnectError("boom")

    monkeypatch.setattr(forge._http, "put", boom)
    assert forge.enable_vulnerability_alerts() is False


# --- enable_automated_security_fixes ---


def test_enable_automated_security_fixes_success(tmp_path, monkeypatch):
    """204 No Content → True."""
    forge = _forge(tmp_path)

    def fake_put(path):
        assert "/automated-security-fixes" in path
        return _resp(204)

    monkeypatch.setattr(forge._http, "put", fake_put)
    assert forge.enable_automated_security_fixes() is True


def test_enable_automated_security_fixes_403_returns_false(tmp_path, monkeypatch):
    """403 → False."""
    forge = _forge(tmp_path)
    monkeypatch.setattr(forge._http, "put", lambda path: _resp(403))
    assert forge.enable_automated_security_fixes() is False


def test_enable_automated_security_fixes_transport_error_returns_false(
    tmp_path, monkeypatch
):
    """Transport error → False."""
    forge = _forge(tmp_path)

    def boom(path):
        raise real_httpx.ConnectError("boom")

    monkeypatch.setattr(forge._http, "put", boom)
    assert forge.enable_automated_security_fixes() is False


# --- ensure_dependency_graph_enabled ---


def test_ensure_dependency_graph_enabled_delegates(tmp_path, monkeypatch):
    """ensure_dependency_graph_enabled calls enable_vulnerability_alerts."""
    forge = _forge(tmp_path)
    paths_called: list[str] = []

    def fake_put(path):
        paths_called.append(path)
        return _resp(204)

    monkeypatch.setattr(forge._http, "put", fake_put)
    result = forge.ensure_dependency_graph_enabled()
    assert result is True
    assert len(paths_called) == 1
    assert "/vulnerability-alerts" in paths_called[0]


def test_ensure_dependency_graph_enabled_failure_propagates(tmp_path, monkeypatch):
    """When vulnerability-alerts fails, dependency_graph reports False."""
    forge = _forge(tmp_path)
    monkeypatch.setattr(forge._http, "put", lambda path: _resp(403))
    assert forge.ensure_dependency_graph_enabled() is False
