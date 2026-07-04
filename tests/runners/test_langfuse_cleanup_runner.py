"""Unit tests for ``langfuse_cleanup_runner.run_langfuse_cleanup_pass``.

All HTTP I/O is mocked via ``httpx.MockTransport`` so the suite is
hermetic — no real Langfuse instance is required and conftest's HTTP
guard isn't tripped.
"""

from __future__ import annotations

import json

import httpx

from robotsix_mill.config import RepoConfig, Settings, _reset_secrets, Secrets
from robotsix_mill import config as _cfg
from robotsix_mill.runners.langfuse_cleanup_runner import (
    CleanupResult,
    run_langfuse_cleanup_pass,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _settings(tmp_path):
    return Settings(data_dir=str(tmp_path / "data"))


def _repo(public="pk-test", secret="sk-test", base="https://lf.example.com"):
    return RepoConfig(
        repo_id="r",
        langfuse_project_name="proj",
        langfuse_public_key=public,
        langfuse_secret_key=secret,
        langfuse_base_url=base,
    )


def _patch_httpx(monkeypatch, handler):
    """Replace ``httpx.Client`` with one that uses *handler* as transport.
    *handler* takes an httpx.Request and returns an httpx.Response."""
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def _patched_client(**kwargs):
        kwargs["transport"] = transport
        return real_client(**kwargs)

    # httpx is imported lazily inside the runner, so the patch must
    # land on the live httpx module — not on the runner's namespace.
    monkeypatch.setattr("httpx.Client", _patched_client)


# ---------------------------------------------------------------------------
# credential / guard short-circuits
# ---------------------------------------------------------------------------


def test_returns_empty_when_credentials_missing(tmp_path, monkeypatch):
    """No public/secret key → log + return zero-deletions result, no HTTP."""
    s = _settings(tmp_path)
    rc = _repo(public="", secret="")
    called = []
    _patch_httpx(
        monkeypatch,
        lambda req: called.append(req) or httpx.Response(200),
    )
    out = run_langfuse_cleanup_pass(
        settings=s,
        repo_config=rc,
        max_traces=100,
    )
    assert out.traces_deleted == 0
    assert out.project == "r"
    # No HTTP call should have happened — the guard fires before httpx.Client.
    assert called == []


def test_returns_empty_when_max_traces_non_positive(tmp_path, monkeypatch):
    """max_traces ≤ 0 → no-op, no HTTP."""
    s = _settings(tmp_path)
    rc = _repo()
    called = []
    _patch_httpx(
        monkeypatch,
        lambda req: called.append(req) or httpx.Response(200),
    )
    out = run_langfuse_cleanup_pass(
        settings=s,
        repo_config=rc,
        max_traces=0,
    )
    assert out.traces_deleted == 0
    assert called == []


def test_uses_global_secrets_when_repo_config_none(tmp_path, monkeypatch):
    """repo_config=None → falls back to get_secrets() and labels 'default'."""
    s = _settings(tmp_path)
    _reset_secrets()
    _cfg._secrets = Secrets(
        langfuse_base_url="https://lf.example.com",
        langfuse_public_key="pk-default",
        langfuse_secret_key="sk-default",
    )

    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(
            200,
            json={"meta": {"totalItems": 10}, "data": []},
        )

    _patch_httpx(monkeypatch, handler)
    out = run_langfuse_cleanup_pass(
        settings=s,
        repo_config=None,
        max_traces=50,
    )
    assert out.project == "default"
    # total (10) ≤ max (50) → no DELETE
    assert all(req.method == "GET" for req in seen)
    _reset_secrets()


# ---------------------------------------------------------------------------
# happy-path traversal
# ---------------------------------------------------------------------------


def test_under_cap_returns_without_deleting(tmp_path, monkeypatch):
    """totalItems ≤ max_traces → no DELETE, traces_deleted=0."""
    s = _settings(tmp_path)
    rc = _repo()
    seen_methods = []

    def handler(req):
        seen_methods.append(req.method)
        return httpx.Response(
            200,
            json={"meta": {"totalItems": 50}, "data": []},
        )

    _patch_httpx(monkeypatch, handler)
    out = run_langfuse_cleanup_pass(
        settings=s,
        repo_config=rc,
        max_traces=100,
    )
    assert out.traces_before == 50
    assert out.traces_deleted == 0
    # one GET for count, no DELETE
    assert "DELETE" not in seen_methods


def test_over_cap_deletes_oldest_until_under(tmp_path, monkeypatch):
    """total=250, cap=200 → must delete 50 in batches of ≤100."""
    s = _settings(tmp_path)
    rc = _repo()
    deletes: list[list] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            # First call may be the count (limit=1). Subsequent are list.
            qs = dict(httpx.QueryParams(req.url.query.decode()))
            if qs.get("limit") == "1":
                return httpx.Response(
                    200,
                    json={"meta": {"totalItems": 250}, "data": []},
                )
            # listing: return up to 'limit' ids
            limit = int(qs["limit"])
            ids = [{"id": f"trace-{i}"} for i in range(limit)]
            return httpx.Response(200, json={"data": ids})
        if req.method == "DELETE":
            body = json.loads(req.content)
            deletes.append(body["traceIds"])
            return httpx.Response(204)
        return httpx.Response(500)

    _patch_httpx(monkeypatch, handler)
    out = run_langfuse_cleanup_pass(
        settings=s,
        repo_config=rc,
        max_traces=200,
    )
    assert out.traces_before == 250
    assert out.traces_deleted == 50
    # one batch (50 ≤ _PAGE_SIZE=100)
    assert len(deletes) == 1
    assert len(deletes[0]) == 50


def test_empty_list_response_breaks_loop(tmp_path, monkeypatch):
    """If list returns empty ids before we've deleted to_delete, log
    + break, return partial."""
    s = _settings(tmp_path)
    rc = _repo()
    state = {"deleted_already": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            qs = dict(httpx.QueryParams(req.url.query.decode()))
            if qs.get("limit") == "1":
                return httpx.Response(
                    200,
                    json={"meta": {"totalItems": 300}, "data": []},
                )
            # First list returns 100, second returns empty.
            if state["deleted_already"] == 0:
                ids = [{"id": f"t-{i}"} for i in range(100)]
                return httpx.Response(200, json={"data": ids})
            return httpx.Response(200, json={"data": []})
        if req.method == "DELETE":
            state["deleted_already"] += 100
            return httpx.Response(204)
        return httpx.Response(500)

    _patch_httpx(monkeypatch, handler)
    out = run_langfuse_cleanup_pass(
        settings=s,
        repo_config=rc,
        max_traces=200,
    )
    assert out.traces_deleted == 100  # only one batch, then empty list


# ---------------------------------------------------------------------------
# error handling
# ---------------------------------------------------------------------------


def test_count_http_failure_returns_zero_deleted(tmp_path, monkeypatch, caplog):
    """5xx on the count call → caught, returns partial result, never raises."""
    s = _settings(tmp_path)
    rc = _repo()
    _patch_httpx(
        monkeypatch,
        lambda req: httpx.Response(503, text="upstream down"),
    )
    out = run_langfuse_cleanup_pass(
        settings=s,
        repo_config=rc,
        max_traces=100,
    )
    assert out.traces_deleted == 0


def test_delete_failure_preserves_partial_count(tmp_path, monkeypatch):
    """DELETE 5xx mid-loop → exception caught, deleted_total preserved."""
    s = _settings(tmp_path)
    rc = _repo()
    state = {"delete_calls": 0}

    def handler(req):
        if req.method == "GET":
            qs = dict(httpx.QueryParams(req.url.query.decode()))
            if qs.get("limit") == "1":
                return httpx.Response(
                    200,
                    json={"meta": {"totalItems": 300}, "data": []},
                )
            ids = [{"id": f"t-{i}"} for i in range(100)]
            return httpx.Response(200, json={"data": ids})
        if req.method == "DELETE":
            state["delete_calls"] += 1
            if state["delete_calls"] == 2:
                return httpx.Response(500)
            return httpx.Response(204)
        return httpx.Response(500)

    _patch_httpx(monkeypatch, handler)
    out = run_langfuse_cleanup_pass(
        settings=s,
        repo_config=rc,
        max_traces=200,
    )
    # First batch succeeded, second crashed mid-call.
    assert out.traces_deleted == 100


# ---------------------------------------------------------------------------
# auth header construction
# ---------------------------------------------------------------------------


def test_basic_auth_header_built_from_keys(tmp_path, monkeypatch):
    """The Authorization header is `Basic base64(pk:sk)`."""
    s = _settings(tmp_path)
    rc = _repo(public="pk-abc", secret="sk-xyz")

    captured = {}

    def handler(req):
        captured["auth"] = req.headers.get("authorization", "")
        return httpx.Response(
            200,
            json={"meta": {"totalItems": 0}, "data": []},
        )

    _patch_httpx(monkeypatch, handler)
    run_langfuse_cleanup_pass(
        settings=s,
        repo_config=rc,
        max_traces=10,
    )
    import base64

    expected = "Basic " + base64.b64encode(b"pk-abc:sk-xyz").decode()
    assert captured["auth"] == expected


def test_uses_langfuse_base_url_from_repo_config(tmp_path, monkeypatch):
    """The configured base URL is used (not cloud.langfuse.com)."""
    s = _settings(tmp_path)
    rc = _repo(base="https://lf.custom.example.com")
    captured = {}

    def handler(req):
        captured["url"] = str(req.url)
        return httpx.Response(
            200,
            json={"meta": {"totalItems": 0}, "data": []},
        )

    _patch_httpx(monkeypatch, handler)
    run_langfuse_cleanup_pass(
        settings=s,
        repo_config=rc,
        max_traces=10,
    )
    assert captured["url"].startswith("https://lf.custom.example.com/")


# ---------------------------------------------------------------------------
# dataclass shape
# ---------------------------------------------------------------------------


def test_cleanup_result_fields():
    r = CleanupResult(project="x", traces_before=100, traces_deleted=10)
    assert r.project == "x"
    assert r.traces_before == 100
    assert r.traces_deleted == 10
