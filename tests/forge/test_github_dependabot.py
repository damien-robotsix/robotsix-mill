"""Tests for the GitHubForge Dependabot vulnerability-alert mixin.

Monkeypatches ``forge._http.get`` directly so the tests exercise
``list_dependabot_alerts`` (normalization, pagination, error degradation)
without a real HTTP transport.
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
    # FORGE_TOKEN is now a Secrets-only field; pop before Settings()
    kw.pop("FORGE_TOKEN", None)
    s = Settings(**kw)
    _set_secrets(forge_token="tok")
    return GitHubForge(s)


def _resp(status_code, json_data):
    return type(
        "FakeResponse",
        (),
        {
            "status_code": status_code,
            "_json": json_data,
            "text": "",
            "json": lambda self: self._json,
            "raise_for_status": lambda self: (
                None
                if 200 <= self.status_code < 300
                else (_ for _ in ()).throw(
                    real_httpx.HTTPStatusError(
                        f"HTTP {self.status_code}",
                        request=real_httpx.Request("GET", "http://x"),
                        response=self,
                    )
                )
            ),
        },
    )()


def _raw_alert(number, *, severity="high", ghsa="GHSA-xxxx", package="lodash"):
    return {
        "number": number,
        "state": "open",
        "html_url": f"https://github.com/o/r/security/dependabot/{number}",
        "dependency": {"manifest_path": "package-lock.json"},
        "security_vulnerability": {
            "severity": severity,
            "package": {"ecosystem": "npm", "name": package},
        },
        "security_advisory": {
            "ghsa_id": ghsa,
            "summary": "Prototype pollution",
            "identifiers": [
                {"type": "GHSA", "value": ghsa},
                {"type": "CVE", "value": "CVE-2021-1234"},
            ],
        },
    }


def test_list_dependabot_alerts_normalizes_fields(tmp_path, monkeypatch):
    """A raw alert is flattened into the ingest dict shape."""
    forge = _forge(tmp_path)

    def fake_get(path, params=None):
        assert "dependabot/alerts" in path
        assert params["state"] == "open"
        if params.get("page", 1) == 1:
            return _resp(200, [_raw_alert(1)])
        return _resp(200, [])

    monkeypatch.setattr(forge._http, "get", fake_get)

    alerts = forge.list_dependabot_alerts()
    assert len(alerts) == 1
    a = alerts[0]
    assert a["number"] == 1
    assert a["ghsa_id"] == "GHSA-xxxx"
    assert a["cve_id"] == "CVE-2021-1234"
    assert a["severity"] == "high"
    assert a["package"] == "lodash"
    assert a["ecosystem"] == "npm"
    assert a["manifest_path"] == "package-lock.json"
    assert a["summary"] == "Prototype pollution"
    assert a["url"].endswith("/dependabot/1")


def test_list_dependabot_alerts_returns_empty_on_404(tmp_path, monkeypatch):
    """404 (alerts disabled) degrades to []."""
    forge = _forge(tmp_path)
    monkeypatch.setattr(forge._http, "get", lambda path, params=None: _resp(404, {}))
    assert forge.list_dependabot_alerts() == []


def test_list_dependabot_alerts_returns_empty_on_403(tmp_path, monkeypatch):
    """403 (token lacks permission) degrades to [] — never raises."""
    forge = _forge(tmp_path)
    monkeypatch.setattr(forge._http, "get", lambda path, params=None: _resp(403, {}))
    assert forge.list_dependabot_alerts() == []


def test_list_dependabot_alerts_returns_empty_on_transport_error(tmp_path, monkeypatch):
    """A raised transport error degrades to [] — best-effort."""
    forge = _forge(tmp_path)

    def boom(path, params=None):
        raise real_httpx.ConnectError("boom")

    monkeypatch.setattr(forge._http, "get", boom)
    assert forge.list_dependabot_alerts() == []


def test_list_dependabot_alerts_paginates(tmp_path, monkeypatch):
    """A full page triggers a follow-up page; a short page stops."""
    forge = _forge(tmp_path)
    full_page = [_raw_alert(n, ghsa=f"GHSA-{n}", package=f"pkg{n}") for n in range(100)]

    def fake_get(path, params=None):
        page = params.get("page", 1)
        if page == 1:
            return _resp(200, full_page)
        if page == 2:
            return _resp(200, [_raw_alert(200, ghsa="GHSA-200", package="tail")])
        return _resp(200, [])

    monkeypatch.setattr(forge._http, "get", fake_get)
    alerts = forge.list_dependabot_alerts()
    assert len(alerts) == 101
    assert alerts[-1]["package"] == "tail"
