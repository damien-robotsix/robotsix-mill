"""Unit tests for the shared HTTP transport wrapper ``_ApiClient``.

These exercise the transport wrapper in isolation — ``httpx.Client`` and
the ``headers_factory`` are mocked so we assert URL construction, header
injection, timeout, the per-verb dispatch, the ``client()`` context
manager lifecycle, and the single-retry-on-401 behaviour without any
real network I/O.
"""

from types import SimpleNamespace

import httpx as real_httpx
import pytest

from robotsix_mill.forge._http import _ApiClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.read_count = 0

    def read(self) -> None:
        self.read_count += 1


def _mock_httpx(monkeypatch, responses):
    """Replace ``httpx.Client`` with a recorder that hands out *responses*
    in order.  Returns a ``calls`` list of per-request dicts and an
    ``inits`` list of constructor-kwargs.
    """
    seq = list(responses)
    calls: list[dict] = []
    inits: list[dict] = []

    class MockClient:
        def __init__(self, **kw):
            inits.append(kw)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def _record(self, method, url, headers=None, **kwargs):
            calls.append(
                {
                    "method": method,
                    "url": url,
                    "headers": headers,
                    "kwargs": kwargs,
                }
            )
            return seq.pop(0)

        def get(self, url, headers=None, **kwargs):
            return self._record("get", url, headers, **kwargs)

        def post(self, url, headers=None, **kwargs):
            return self._record("post", url, headers, **kwargs)

        def put(self, url, headers=None, **kwargs):
            return self._record("put", url, headers, **kwargs)

        def delete(self, url, headers=None, **kwargs):
            return self._record("delete", url, headers, **kwargs)

    monkeypatch.setattr(real_httpx, "Client", MockClient)
    return calls, inits


def _client(api_url="https://api.test.com/", api_attr="github_api_url", factory=None):
    settings = SimpleNamespace(**{api_attr: api_url})
    if factory is None:

        def factory(s, rc):
            return {"Authorization": "Bearer tok"}

    return _ApiClient(settings, None, api_attr, factory)


# ---------------------------------------------------------------------------
# URL construction + header injection + timeout
# ---------------------------------------------------------------------------


def test_get_builds_url_and_injects_headers(monkeypatch):
    calls, inits = _mock_httpx(monkeypatch, [_FakeResponse(200)])
    client = _client()

    resp = client.get("/repos/o/r", params={"page": 1})

    assert resp.status_code == 200
    assert calls[0]["method"] == "get"
    # trailing slash on the api base is stripped before concatenation
    assert calls[0]["url"] == "https://api.test.com/repos/o/r"
    assert calls[0]["headers"] == {"Authorization": "Bearer tok"}
    assert calls[0]["kwargs"] == {"params": {"page": 1}}
    # every request opens a client with the 30s timeout
    assert inits == [{"timeout": 30}]


def test_headers_factory_receives_settings_and_repo_config(monkeypatch):
    _mock_httpx(monkeypatch, [_FakeResponse(200)])
    seen = {}

    def factory(settings, repo_config):
        seen["settings"] = settings
        seen["repo_config"] = repo_config
        return {"X-Test": "1"}

    settings = SimpleNamespace(github_api_url="https://api.test.com")
    client = _ApiClient(settings, None, "github_api_url", factory)
    client.get("/x")

    assert seen["settings"] is settings
    assert seen["repo_config"] is None


def test_response_body_is_buffered(monkeypatch):
    resp = _FakeResponse(200)
    _mock_httpx(monkeypatch, [resp])
    _client().get("/x")
    assert resp.read_count == 1


# ---------------------------------------------------------------------------
# Per-verb dispatch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["get", "post", "put", "delete"])
def test_verb_dispatch(monkeypatch, verb):
    calls, _ = _mock_httpx(monkeypatch, [_FakeResponse(204)])
    client = _client()
    getattr(client, verb)("/path")
    assert calls[0]["method"] == verb


def test_request_lowercases_method(monkeypatch):
    calls, _ = _mock_httpx(monkeypatch, [_FakeResponse(200)])
    _client().request("POST", "/path", json={"k": "v"})
    assert calls[0]["method"] == "post"
    assert calls[0]["kwargs"] == {"json": {"k": "v"}}


# ---------------------------------------------------------------------------
# regenerate_headers
# ---------------------------------------------------------------------------


def test_regenerate_headers_reruns_factory():
    counter = {"n": 0}

    def factory(s, rc):
        counter["n"] += 1
        return {"call": str(counter["n"])}

    client = _client(factory=factory)
    assert client.regenerate_headers() == {"call": "1"}
    assert client.regenerate_headers() == {"call": "2"}


# ---------------------------------------------------------------------------
# client() context manager
# ---------------------------------------------------------------------------


def test_client_context_manager_yields_triplet(monkeypatch):
    _, inits = _mock_httpx(monkeypatch, [])
    client = _client(api_url="https://api.test.com/")

    with client.client() as (c, api_base, headers):
        assert isinstance(c, real_httpx.Client)
        assert api_base == "https://api.test.com"
        assert headers == {"Authorization": "Bearer tok"}

    assert inits == [{"timeout": 30}]


# ---------------------------------------------------------------------------
# 401 retry behaviour
# ---------------------------------------------------------------------------


def test_401_invalidates_and_retries_once(monkeypatch):
    monkeypatch.setattr("robotsix_mill.forge._http.time.sleep", lambda _s: None)
    calls, _ = _mock_httpx(monkeypatch, [_FakeResponse(401), _FakeResponse(200)])

    headers_seq = [{"Authorization": "stale"}, {"Authorization": "fresh"}]

    def factory(s, rc):
        return headers_seq.pop(0)

    invalidated = {"n": 0}

    client = _client(factory=factory)
    client._on_401 = lambda: invalidated.__setitem__("n", invalidated["n"] + 1)

    resp = client.get("/x")

    assert resp.status_code == 200
    assert invalidated["n"] == 1
    assert len(calls) == 2
    # first attempt uses stale headers, retry uses freshly regenerated ones
    assert calls[0]["headers"] == {"Authorization": "stale"}
    assert calls[1]["headers"] == {"Authorization": "fresh"}


def test_401_without_callback_is_returned_as_is(monkeypatch):
    calls, _ = _mock_httpx(monkeypatch, [_FakeResponse(401)])
    client = _client()  # _on_401 stays None

    resp = client.get("/x")

    assert resp.status_code == 401
    assert len(calls) == 1


def test_second_401_returned_after_single_retry(monkeypatch):
    monkeypatch.setattr("robotsix_mill.forge._http.time.sleep", lambda _s: None)
    calls, _ = _mock_httpx(monkeypatch, [_FakeResponse(401), _FakeResponse(401)])
    client = _client()
    client._on_401 = lambda: None

    resp = client.get("/x")

    assert resp.status_code == 401
    assert len(calls) == 2  # retried exactly once, no third attempt
