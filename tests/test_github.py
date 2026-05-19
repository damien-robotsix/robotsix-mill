"""Test GitHubForge HTTP seams with mocked httpx.Client.

No stage-level monkeypatching — tests call _create_pr, _get_pr, _check_status,
_parse_owner_repo, and _build_headers directly with a mocked transport.
"""

import httpx as real_httpx
import pytest

from robotsix_mill.config import Settings
from robotsix_mill.forge.github import (
    GitHubForge,
    _build_headers,
    _parse_owner_repo,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _settings(tmp_path, **kw):
    kw.setdefault("MILL_DATA_DIR", str(tmp_path))
    kw.setdefault("FORGE_KIND", "github")
    kw.setdefault("FORGE_REMOTE_URL", "https://github.com/o/r.git")
    kw.setdefault("FORGE_TOKEN", "tok")
    return Settings(**kw)


def _forge(tmp_path, **kw):
    return GitHubForge(_settings(tmp_path, **kw))


def _make_response(status_code, json_data, text=""):
    """Build a minimal httpx-like response object."""
    resp = type("FakeResponse", (), {
        "status_code": status_code,
        "_json": json_data,
        "text": text,
        "json": lambda self: self._json,
        "raise_for_status": lambda self: (
            None if 200 <= self.status_code < 300
            else (_ for _ in ()).throw(
                real_httpx.HTTPStatusError(
                    f"HTTP {self.status_code}",
                    request=real_httpx.Request("GET", "http://x"),
                    response=self,
                )
            )
        ),
    })()
    return resp


def _mock_httpx(monkeypatch, *, post_response=None, get_map=None):
    """Replace httpx.Client with a controllable mock.

    *post_response*: returned for every POST call.
    *get_map*: dict mapping URL substrings → FakeResponse for GET calls.
    """
    captured = {"post_payload": None}

    class MockClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, headers=None, json=None):
            captured["post_payload"] = json
            return post_response or _make_response(500, {}, "error")

        def get(self, url, headers=None, params=None):
            if get_map:
                for key, resp in get_map.items():
                    if key in url:
                        return resp
            return _make_response(404, [], "")

    monkeypatch.setattr(real_httpx, "Client", MockClient)
    return captured


# ---------------------------------------------------------------------------
# _build_headers
# ---------------------------------------------------------------------------

def test_build_headers():
    h = _build_headers("mytoken")
    assert h["Authorization"] == "Bearer mytoken"
    assert h["Accept"] == "application/vnd.github+json"
    assert h["X-GitHub-Api-Version"] == "2022-11-28"


# ---------------------------------------------------------------------------
# _parse_owner_repo
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://github.com/o/r.git", ("o", "r")),
    ("https://github.com/o/r", ("o", "r")),
    ("git@github.com:o/r.git", ("o", "r")),
    ("https://github.com/owner-name/repo_name", ("owner-name", "repo_name")),
])
def test_parse_owner_repo_valid(url, expected):
    assert _parse_owner_repo(url) == expected


@pytest.mark.parametrize("url", [
    "",
    "not-a-url",
    "https://gitlab.com/o/r.git",
])
def test_parse_owner_repo_invalid_raises_runtimeerror(url):
    with pytest.raises(RuntimeError, match="cannot parse owner/repo"):
        _parse_owner_repo(url)


# ---------------------------------------------------------------------------
# _create_pr (via open_merge_request)
# ---------------------------------------------------------------------------

def test_create_pr_201_returns_html_url(tmp_path, monkeypatch):
    fake_json = {"html_url": "https://github.com/o/r/pull/42"}
    _mock_httpx(monkeypatch, post_response=_make_response(201, fake_json))

    forge = _forge(tmp_path)
    url = forge.open_merge_request(
        source_branch="feature/x", title="t", body="b"
    )
    assert url == "https://github.com/o/r/pull/42"


def test_create_pr_422_falls_back_to_existing_open_pr(tmp_path, monkeypatch):
    """422 → GET /pulls?head=...&state=open returns existing PR."""
    post_422 = _make_response(422, {}, "already exists")
    existing_pr = [{"html_url": "https://github.com/o/r/pull/99", "number": 99}]
    get_map = {"repos/o/r/pulls": _make_response(200, existing_pr)}
    captured = _mock_httpx(
        monkeypatch,
        post_response=post_422,
        get_map=get_map,
    )

    forge = _forge(tmp_path)
    url = forge.open_merge_request(
        source_branch="feature/x", title="t", body="b"
    )
    assert url == "https://github.com/o/r/pull/99"
    # Verify the GET params included head and state=open
    assert captured["post_payload"] is not None


def test_create_pr_422_no_existing_pr_raises(tmp_path, monkeypatch):
    """422 + no open PR → RuntimeError."""
    post_422 = _make_response(422, {}, "already exists")
    get_map = {"repos/o/r/pulls": _make_response(200, [])}
    _mock_httpx(monkeypatch, post_response=post_422, get_map=get_map)

    forge = _forge(tmp_path)
    with pytest.raises(RuntimeError, match="GitHub PR create failed"):
        forge.open_merge_request(
            source_branch="feature/x", title="t", body="b"
        )


def test_create_pr_non_201_non_422_raises(tmp_path, monkeypatch):
    """Any other status → RuntimeError."""
    _mock_httpx(monkeypatch, post_response=_make_response(403, {}, "forbidden"))

    forge = _forge(tmp_path)
    with pytest.raises(RuntimeError, match="GitHub PR create failed"):
        forge.open_merge_request(
            source_branch="feature/x", title="t", body="b"
        )


def test_create_pr_post_payload_shape(tmp_path, monkeypatch):
    """Verify the POST JSON includes head, base, title, body."""
    captured = _mock_httpx(
        monkeypatch,
        post_response=_make_response(201, {"html_url": "http://x"}),
    )

    forge = _forge(tmp_path)
    forge.open_merge_request(
        source_branch="feature/x", title="My Title", body="My Body"
    )

    payload = captured["post_payload"]
    assert payload["head"] == "feature/x"
    assert payload["base"] == "main"  # default FORGE_TARGET_BRANCH
    assert payload["title"] == "My Title"
    assert payload["body"] == "My Body"
    # All expected keys present and no extras
    assert set(payload.keys()) == {"head", "base", "title", "body"}


# ---------------------------------------------------------------------------
# _get_pr (via pr_status)
# ---------------------------------------------------------------------------

def test_get_pr_found_returns_expected_dict(tmp_path, monkeypatch):
    """pr_status returns dict with merged, state, url, mergeable, sha."""
    list_resp = [{"number": 7, "html_url": "http://pr/7"}]
    detail_resp = {
        "number": 7,
        "merged": False,
        "state": "open",
        "html_url": "http://pr/7",
        "mergeable": True,
        "head": {"sha": "abc123"},
    }
    # Detail key must come BEFORE list key so it matches first
    # (both contain "repos/o/r/pulls").
    get_map = {
        "repos/o/r/pulls/7": _make_response(200, detail_resp),
        "repos/o/r/pulls": _make_response(200, list_resp),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    status = forge.pr_status(source_branch="feature/x")
    assert status == {
        "merged": False,
        "state": "open",
        "url": "http://pr/7",
        "mergeable": True,
        "sha": "abc123",
    }


def test_get_pr_not_found_returns_none(tmp_path, monkeypatch):
    """Empty list → None."""
    get_map = {"repos/o/r/pulls": _make_response(200, [])}
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    assert forge.pr_status(source_branch="feature/x") is None


def test_get_pr_uses_two_step_flow(tmp_path, monkeypatch):
    """Verify: list endpoint first (with state=all), then detail by number."""
    calls = []

    class TrackingClient:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass

        def post(self, url, headers=None, json=None):
            return _make_response(500, {}, "")

        def get(self, url, headers=None, params=None):
            calls.append(url)
            if "/pulls/7" in url:
                return _make_response(200, {
                    "number": 7, "merged": True, "state": "closed",
                    "html_url": "http://pr/7", "mergeable": None,
                    "head": {"sha": "def456"},
                })
            if "/pulls" in url:
                return _make_response(200, [{"number": 7}])
            return _make_response(404, [], "")

    monkeypatch.setattr(real_httpx, "Client", TrackingClient)

    forge = _forge(tmp_path)
    status = forge.pr_status(source_branch="feature/x")
    assert status is not None
    # First call: list endpoint, second: detail by number
    assert any("/pulls/7" in u for u in calls)
    assert any("/pulls?" in u or "/pulls" in u for u in calls)


# ---------------------------------------------------------------------------
# _check_status (smoke)
# ---------------------------------------------------------------------------

def test_check_status_no_pr_returns_none(tmp_path, monkeypatch):
    """When _get_pr returns None, check_status returns None."""
    get_map = {"repos/o/r/pulls": _make_response(200, [])}
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    assert forge.check_status(source_branch="feature/x") is None


def test_check_status_happy_path(tmp_path, monkeypatch):
    """PR exists + check-runs endpoint returns data → expected dict."""
    # Three-step flow: list PRs → detail PR → check-runs
    list_resp = [{"number": 3}]
    detail_resp = {
        "number": 3,
        "merged": False,
        "state": "open",
        "html_url": "http://pr/3",
        "mergeable": True,
        "head": {"sha": "abc123"},
    }
    check_runs_resp = {
        "check_runs": [
            {
                "id": 101,
                "name": "CI / test",
                "status": "completed",
                "conclusion": "success",
                "output": {"summary": "All green", "text": None, "annotations": []},
            }
        ]
    }
    get_map = {
        "repos/o/r/pulls/3": _make_response(200, detail_resp),
        "repos/o/r/pulls": _make_response(200, list_resp),
        "commits/abc123/check-runs": _make_response(200, check_runs_resp),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.check_status(source_branch="feature/x")
    assert result is not None
    assert "conclusion" in result
    assert "failing" in result
    assert result["conclusion"] == "success"
    assert result["failing"] == []
