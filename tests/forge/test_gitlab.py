"""Test GitLabForge HTTP seams with mocked httpx.Client.

No stage-level monkeypatching — tests call _create_mr, _find_mr, _get_latest_pipeline,
_parse_gitlab_project_path, and _build_headers directly with a mocked transport.
"""

import httpx as real_httpx
import pytest

from robotsix_mill.config import Settings
from robotsix_mill.forge.gitlab import (
    GitLabForge,
    _build_headers,
    _parse_gitlab_project_path,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _settings(tmp_path, **kw):
    kw.setdefault("MILL_DATA_DIR", str(tmp_path))
    kw.setdefault("FORGE_KIND", "gitlab")
    kw.setdefault("FORGE_REMOTE_URL", "https://gitlab.com/ns/project.git")
    kw.setdefault("FORGE_TOKEN", "glpat-token")
    return Settings(**kw)


def _forge(tmp_path, **kw):
    return GitLabForge(_settings(tmp_path, **kw))


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


def _mock_httpx(monkeypatch, *, get_map=None, post_response=None, put_response=None):
    """Replace httpx.Client with a controllable mock.

    *get_map*: dict mapping URL substrings → FakeResponse for GET calls.
    *post_response*: returned for every POST call.
    *put_response*: returned for every PUT call.
    """
    captured = {"post_payload": None, "put_payload": None}

    class MockClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, headers=None, json=None, **kwargs):
            captured["post_payload"] = json
            return post_response or _make_response(500, {}, "error")

        def put(self, url, headers=None, json=None, **kwargs):
            captured["put_payload"] = json
            return put_response or _make_response(500, {}, "error")

        def get(self, url, headers=None, params=None, **kwargs):
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
    h = _build_headers("glpat-mytoken")
    assert h["PRIVATE-TOKEN"] == "glpat-mytoken"


# ---------------------------------------------------------------------------
# _parse_gitlab_project_path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://gitlab.com/ns/project.git", "ns/project"),
    ("https://gitlab.com/ns/project", "ns/project"),
    ("git@gitlab.com:ns/project.git", "ns/project"),
    ("git@gitlab.com:ns/project", "ns/project"),
    ("https://gitlab.com/group/subgroup/proj.git", "group/subgroup/proj"),
    ("git@gitlab.com:group/subgroup/proj.git", "group/subgroup/proj"),
    # Self-hosted GitLab instances
    ("https://gitlab.mycompany.com/ns/project.git", "ns/project"),
    ("https://gitlab.mycompany.com/ns/project", "ns/project"),
    ("git@gitlab.mycompany.com:ns/project.git", "ns/project"),
    ("git@gitlab.mycompany.com:ns/project", "ns/project"),
    ("https://gitlab.internal.example.com/group/subgroup/proj.git", "group/subgroup/proj"),
    ("git@gitlab.internal.example.com:group/subgroup/proj.git", "group/subgroup/proj"),
])
def test_parse_gitlab_project_path_valid(url, expected):
    assert _parse_gitlab_project_path(url) == expected


@pytest.mark.parametrize("url", [
    "",
    "not-a-url",
    "/absolute/path/to/repo.git",
    "git://invalid-protocol.example.com/ns/project.git",
])
def test_parse_gitlab_project_path_invalid_raises_runtimeerror(url):
    with pytest.raises(RuntimeError, match="cannot parse GitLab project path"):
        _parse_gitlab_project_path(url)


# ---------------------------------------------------------------------------
# open_merge_request
# ---------------------------------------------------------------------------

def test_create_mr_201_returns_web_url(tmp_path, monkeypatch):
    """201 from POST → return web_url."""
    project_json = {"id": 42}
    mr_json = {"web_url": "https://gitlab.com/ns/project/-/merge_requests/1"}
    get_map = {"projects/ns%2Fproject": _make_response(200, project_json)}
    captured = _mock_httpx(
        monkeypatch,
        get_map=get_map,
        post_response=_make_response(201, mr_json),
    )

    forge = _forge(tmp_path)
    url = forge.open_merge_request(
        source_branch="feature/x", title="t", body="b"
    )
    assert url == "https://gitlab.com/ns/project/-/merge_requests/1"
    assert captured["post_payload"]["source_branch"] == "feature/x"
    assert captured["post_payload"]["target_branch"] == "main"
    assert captured["post_payload"]["title"] == "t"
    assert captured["post_payload"]["description"] == "b"


def test_create_mr_409_falls_back_to_existing_mr(tmp_path, monkeypatch):
    """409 → find MR by source_branch and return its web_url."""
    project_json = {"id": 42}
    post_409 = _make_response(409, {}, "already exists")
    mr_list = [{"iid": 7, "web_url": "https://gitlab.com/ns/project/-/merge_requests/7", "state": "opened"}]
    get_map = {
        "merge_requests/7/pipelines": _make_response(200, []),
        "merge_requests": _make_response(200, mr_list),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(
        monkeypatch,
        get_map=get_map,
        post_response=post_409,
    )

    forge = _forge(tmp_path)
    url = forge.open_merge_request(
        source_branch="feature/x", title="t", body="b"
    )
    assert url == "https://gitlab.com/ns/project/-/merge_requests/7"


def test_create_mr_409_no_existing_mr_raises(tmp_path, monkeypatch):
    """409 + no existing MR → RuntimeError."""
    project_json = {"id": 42}
    post_409 = _make_response(409, {}, "already exists")
    get_map = {
        "merge_requests": _make_response(200, []),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(
        monkeypatch,
        get_map=get_map,
        post_response=post_409,
    )

    forge = _forge(tmp_path)
    with pytest.raises(RuntimeError, match="GitLab MR create failed"):
        forge.open_merge_request(
            source_branch="feature/x", title="t", body="b"
        )


def test_create_mr_409_find_mr_raises_propagates_as_runtimeerror(tmp_path, monkeypatch):
    """409 + _find_mr raises → RuntimeError (not a raw HTTPStatusError)."""
    project_json = {"id": 42}
    post_409 = _make_response(409, {}, "already exists")
    # _find_mr calls GET merge_requests → 500 triggers raise_for_status
    get_map = {
        "merge_requests": _make_response(500, {}, "internal error"),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(
        monkeypatch,
        get_map=get_map,
        post_response=post_409,
    )

    forge = _forge(tmp_path)
    with pytest.raises(RuntimeError, match="GitLab MR create failed"):
        forge.open_merge_request(
            source_branch="feature/x", title="t", body="b"
        )


def test_create_mr_unexpected_status_raises(tmp_path, monkeypatch):
    """Any non-201/409 status → RuntimeError."""
    project_json = {"id": 42}
    get_map = {"projects/ns%2Fproject": _make_response(200, project_json)}
    _mock_httpx(
        monkeypatch,
        get_map=get_map,
        post_response=_make_response(403, {}, "forbidden"),
    )

    forge = _forge(tmp_path)
    with pytest.raises(RuntimeError, match="GitLab MR create failed"):
        forge.open_merge_request(
            source_branch="feature/x", title="t", body="b"
        )


# ---------------------------------------------------------------------------
# pr_status
# ---------------------------------------------------------------------------

def test_pr_status_mr_found_returns_expected_dict(tmp_path, monkeypatch):
    """MR found → return standard status dict."""
    project_json = {"id": 42}
    mr = {
        "iid": 7,
        "state": "opened",
        "web_url": "https://gitlab.com/ns/project/-/merge_requests/7",
        "merge_status": "can_be_merged",
        "sha": "abc123",
        "diff_refs": {"head_sha": "abc123"},
    }
    get_map = {
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    status = forge.pr_status(source_branch="feature/x")
    assert status == {
        "merged": False,
        "state": "opened",
        "url": "https://gitlab.com/ns/project/-/merge_requests/7",
        "mergeable": True,
        "sha": "abc123",
        "number": 7,
    }


def test_pr_status_no_mr_returns_none(tmp_path, monkeypatch):
    """No MR found → None."""
    project_json = {"id": 42}
    get_map = {
        "merge_requests": _make_response(200, []),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    assert forge.pr_status(source_branch="feature/x") is None


@pytest.mark.parametrize("merge_status,expected", [
    ("can_be_merged", True),
    ("cannot_be_merged", False),
    ("checking", None),
    ("unchecked", None),
])
def test_pr_status_mergeable_mapping(tmp_path, monkeypatch, merge_status, expected):
    """Verify merge_status → mergeable mapping."""
    project_json = {"id": 42}
    mr = {
        "iid": 1,
        "state": "opened",
        "web_url": "http://x",
        "merge_status": merge_status,
        "sha": "abc",
        "diff_refs": {"head_sha": "abc"},
    }
    get_map = {
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    status = forge.pr_status(source_branch="feature/x")
    assert status["mergeable"] == expected


def test_pr_status_merged_mr(tmp_path, monkeypatch):
    """Merged MR → merged=True."""
    project_json = {"id": 42}
    mr = {
        "iid": 1,
        "state": "merged",
        "web_url": "http://x",
        "merge_status": "can_be_merged",
        "sha": "abc",
        "diff_refs": {"head_sha": "abc"},
    }
    get_map = {
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    status = forge.pr_status(source_branch="feature/x")
    assert status["merged"] is True


# ---------------------------------------------------------------------------
# check_status
# ---------------------------------------------------------------------------

def test_check_status_no_mr_returns_none(tmp_path, monkeypatch):
    """When _find_mr returns None, check_status returns None."""
    project_json = {"id": 42}
    get_map = {
        "merge_requests": _make_response(200, []),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    assert forge.check_status(source_branch="feature/x") is None


def test_check_status_pipeline_success(tmp_path, monkeypatch):
    """Pipeline status=success → conclusion=success."""
    project_json = {"id": 42}
    mr = {"iid": 7, "state": "opened", "web_url": "http://x", "merge_status": "can_be_merged", "sha": "abc", "diff_refs": {"head_sha": "abc"}}
    pipeline = {"id": 100, "status": "success"}
    get_map = {
        "merge_requests/7/pipelines": _make_response(200, [pipeline]),
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.check_status(source_branch="feature/x")
    assert result == {"conclusion": "success", "failing": []}


def test_check_status_pipeline_failure(tmp_path, monkeypatch):
    """Pipeline status=failed → conclusion=failure with failed jobs."""
    project_json = {"id": 42}
    mr = {"iid": 7, "state": "opened", "web_url": "http://x", "merge_status": "can_be_merged", "sha": "abc", "diff_refs": {"head_sha": "abc"}}
    pipeline = {"id": 100, "status": "failed"}
    failed_jobs = [
        {"name": "build", "stage": "test"},
        {"name": "lint", "stage": "test"},
    ]
    get_map = {
        "merge_requests/7/pipelines": _make_response(200, [pipeline]),
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
        "pipelines/100/jobs": _make_response(200, failed_jobs),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.check_status(source_branch="feature/x")
    assert result["conclusion"] == "failure"
    assert len(result["failing"]) == 2
    assert result["failing"][0]["name"] == "build"
    assert result["failing"][0]["annotations"] == []


def test_check_status_pipeline_pending(tmp_path, monkeypatch):
    """Pipeline status=running → conclusion=pending."""
    project_json = {"id": 42}
    mr = {"iid": 7, "state": "opened", "web_url": "http://x", "merge_status": "can_be_merged", "sha": "abc", "diff_refs": {"head_sha": "abc"}}
    pipeline = {"id": 100, "status": "running"}
    get_map = {
        "merge_requests/7/pipelines": _make_response(200, [pipeline]),
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.check_status(source_branch="feature/x")
    assert result == {"conclusion": "pending", "failing": []}


def test_check_status_no_pipeline(tmp_path, monkeypatch):
    """No pipeline at all → conclusion=None."""
    project_json = {"id": 42}
    mr = {"iid": 7, "state": "opened", "web_url": "http://x", "merge_status": "can_be_merged", "sha": "abc", "diff_refs": {"head_sha": "abc"}}
    get_map = {
        "merge_requests/7/pipelines": _make_response(200, []),
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.check_status(source_branch="feature/x")
    assert result == {"conclusion": None, "failing": []}


@pytest.mark.parametrize("status", ["pending", "running", "created", "waiting_for_resource", "preparing", "manual", "scheduled"])
def test_check_status_pipeline_pending_variants(tmp_path, monkeypatch, status):
    """All non-terminal statuses → conclusion=pending."""
    project_json = {"id": 42}
    mr = {"iid": 7, "state": "opened", "web_url": "http://x", "merge_status": "can_be_merged", "sha": "abc", "diff_refs": {"head_sha": "abc"}}
    pipeline = {"id": 100, "status": status}
    get_map = {
        "merge_requests/7/pipelines": _make_response(200, [pipeline]),
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.check_status(source_branch="feature/x")
    assert result["conclusion"] == "pending"


def test_check_status_pipeline_canceled_is_failure(tmp_path, monkeypatch):
    """Pipeline status=canceled → conclusion=failure."""
    project_json = {"id": 42}
    mr = {"iid": 7, "state": "opened", "web_url": "http://x", "merge_status": "can_be_merged", "sha": "abc", "diff_refs": {"head_sha": "abc"}}
    pipeline = {"id": 100, "status": "canceled"}
    get_map = {
        "merge_requests/7/pipelines": _make_response(200, [pipeline]),
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
        "pipelines/100/jobs": _make_response(200, []),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.check_status(source_branch="feature/x")
    assert result["conclusion"] == "failure"


# ---------------------------------------------------------------------------
# merge_pr
# ---------------------------------------------------------------------------

def test_merge_pr_mr_not_found(tmp_path, monkeypatch):
    """No MR → {"merged": False, "reason": "MR not found"}."""
    project_json = {"id": 42}
    get_map = {
        "merge_requests": _make_response(200, []),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.merge_pr(source_branch="feature/x")
    assert result == {"merged": False, "reason": "MR not found"}


def test_merge_pr_success_synchronous_merge(tmp_path, monkeypatch):
    """200 + state=merged → {"merged": True, "reason": "merged"}."""
    project_json = {"id": 42}
    mr = {"iid": 7, "state": "opened", "web_url": "http://x", "merge_status": "can_be_merged", "sha": "abc", "diff_refs": {"head_sha": "abc"}}
    merge_resp = {"state": "merged", "merge_commit_sha": "def456"}
    get_map = {
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    captured = _mock_httpx(
        monkeypatch,
        get_map=get_map,
        put_response=_make_response(200, merge_resp),
    )

    forge = _forge(tmp_path)
    result = forge.merge_pr(source_branch="feature/x")
    assert result == {"merged": True, "reason": "merged"}
    # Verify payload fields
    assert captured["put_payload"]["merge_when_pipeline_succeeds"] is True
    assert captured["put_payload"]["squash"] is True
    assert captured["put_payload"]["should_remove_source_branch"] is False


def test_merge_pr_mwps_set_awaiting_pipeline(tmp_path, monkeypatch):
    """200 + state!=merged → MWPS deferred."""
    project_json = {"id": 42}
    mr = {"iid": 7, "state": "opened", "web_url": "http://x", "merge_status": "can_be_merged", "sha": "abc", "diff_refs": {"head_sha": "abc"}}
    merge_resp = {"state": "opened", "merge_when_pipeline_succeeds": True}
    get_map = {
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(
        monkeypatch,
        get_map=get_map,
        put_response=_make_response(200, merge_resp),
    )

    forge = _forge(tmp_path)
    result = forge.merge_pr(source_branch="feature/x")
    assert result == {
        "merged": False,
        "reason": "merge_when_pipeline_succeeds set; awaiting pipeline",
    }


def test_merge_pr_405_not_allowed(tmp_path, monkeypatch):
    """405 → branch protection reason."""
    project_json = {"id": 42}
    mr = {"iid": 7, "state": "opened", "web_url": "http://x", "merge_status": "can_be_merged", "sha": "abc", "diff_refs": {"head_sha": "abc"}}
    get_map = {
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(
        monkeypatch,
        get_map=get_map,
        put_response=_make_response(405, {}, "not allowed"),
    )

    forge = _forge(tmp_path)
    result = forge.merge_pr(source_branch="feature/x")
    assert result == {"merged": False, "reason": "merge not allowed (branch protection?)"}


def test_merge_pr_409_not_mergeable(tmp_path, monkeypatch):
    """409 → MR not mergeable."""
    project_json = {"id": 42}
    mr = {"iid": 7, "state": "opened", "web_url": "http://x", "merge_status": "can_be_merged", "sha": "abc", "diff_refs": {"head_sha": "abc"}}
    get_map = {
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(
        monkeypatch,
        get_map=get_map,
        put_response=_make_response(409, {}, "conflict"),
    )

    forge = _forge(tmp_path)
    result = forge.merge_pr(source_branch="feature/x")
    assert result == {"merged": False, "reason": "MR is not mergeable"}


def test_merge_pr_network_error(tmp_path, monkeypatch):
    """Network error → {"merged": False} (no raise)."""
    project_json = {"id": 42}
    mr = {"iid": 7, "state": "opened", "web_url": "http://x", "merge_status": "can_be_merged", "sha": "abc", "diff_refs": {"head_sha": "abc"}}
    get_map = {
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }

    class ErrorClient:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, headers=None, json=None, **kwargs):
            return _make_response(500, {}, "")
        def put(self, url, headers=None, json=None, **kwargs):
            raise ConnectionError("connection refused")
        def get(self, url, headers=None, params=None, **kwargs):
            for key, resp in get_map.items():
                if key in url:
                    return resp
            return _make_response(404, [], "")

    monkeypatch.setattr(real_httpx, "Client", ErrorClient)

    forge = _forge(tmp_path)
    result = forge.merge_pr(source_branch="feature/x")
    assert result["merged"] is False
    assert "connection refused" in result["reason"]


def test_merge_pr_unexpected_error_status(tmp_path, monkeypatch):
    """Unexpected HTTP error → {"merged": False, ...}."""
    project_json = {"id": 42}
    mr = {"iid": 7, "state": "opened", "web_url": "http://x", "merge_status": "can_be_merged", "sha": "abc", "diff_refs": {"head_sha": "abc"}}
    get_map = {
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(
        monkeypatch,
        get_map=get_map,
        put_response=_make_response(422, {}, "unprocessable"),
    )

    forge = _forge(tmp_path)
    result = forge.merge_pr(source_branch="feature/x")
    assert result["merged"] is False
    assert "HTTP 422" in result["reason"]


# ---------------------------------------------------------------------------
# merge_pr payload shape
# ---------------------------------------------------------------------------

def test_merge_pr_find_mr_raises_returns_graceful_dict(tmp_path, monkeypatch):
    """When _find_mr raises (e.g. project lookup 500), merge_pr returns
    {"merged": False, "reason": ...} instead of propagating the exception."""
    # Project lookup returns 500 → _resolve_project_id raises RuntimeError
    get_map = {
        "projects/ns%2Fproject": _make_response(500, {}, "internal error"),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.merge_pr(source_branch="feature/x")
    assert result["merged"] is False
    assert "GitLab project lookup failed" in result["reason"]
    assert "500" in result["reason"]


def test_pr_status_api_error_returns_none(tmp_path, monkeypatch):
    """When project lookup fails, pr_status returns None instead of raising."""
    get_map = {
        "projects/ns%2Fproject": _make_response(500, {}, "internal error"),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    assert forge.pr_status(source_branch="feature/x") is None


def test_check_status_api_error_returns_none(tmp_path, monkeypatch):
    """When project lookup fails, check_status returns None instead of raising."""
    get_map = {
        "projects/ns%2Fproject": _make_response(500, {}, "internal error"),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    assert forge.check_status(source_branch="feature/x") is None


def test_merge_pr_payload_includes_squash_and_mwps(tmp_path, monkeypatch):
    """Verify the PUT payload has merge_when_pipeline_succeeds, squash, should_remove_source_branch."""
    project_json = {"id": 42}
    mr = {"iid": 7, "state": "opened", "web_url": "http://x", "merge_status": "can_be_merged", "sha": "abc", "diff_refs": {"head_sha": "abc"}}
    merge_resp = {"state": "merged"}
    get_map = {
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    captured = _mock_httpx(
        monkeypatch,
        get_map=get_map,
        put_response=_make_response(200, merge_resp),
    )

    forge = _forge(tmp_path)
    forge.merge_pr(source_branch="feature/x")
    payload = captured["put_payload"]
    assert payload == {
        "merge_when_pipeline_succeeds": True,
        "squash": True,
        "should_remove_source_branch": False,
    }
