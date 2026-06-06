"""Test GitLabForge HTTP seams with mocked httpx.Client.

No stage-level monkeypatching — tests call _create_mr, _find_mr, _get_latest_pipeline,
_parse_gitlab_project_path, and _build_headers directly with a mocked transport.
"""

import httpx as real_httpx
import pytest

from robotsix_mill.config import Settings, Secrets, _reset_secrets
from robotsix_mill.forge.gitlab import (
    GitLabForge,
    _build_headers,
    _parse_gitlab_project_path,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_secrets(**kw):
    """Populate the Secrets singleton for tests."""
    import robotsix_mill.config as _cfg

    _reset_secrets()
    _cfg._secrets = Secrets(**kw)


def _settings(tmp_path, **kw):
    kw.setdefault("data_dir", str(tmp_path))
    kw.setdefault("FORGE_KIND", "gitlab")
    kw.setdefault("FORGE_REMOTE_URL", "https://gitlab.com/ns/project.git")
    kw.setdefault("FORGE_TOKEN", "glpat-token")
    s = Settings(**kw)
    # Mirror forge_token into Secrets so get_secrets() works
    ft = kw.get("FORGE_TOKEN")
    if ft is not None:
        _set_secrets(forge_token=ft)
    return s


def _forge(tmp_path, **kw):
    return GitLabForge(_settings(tmp_path, **kw))


def _make_response(status_code, json_data, text=""):
    """Build a minimal httpx-like response object."""
    resp = type(
        "FakeResponse",
        (),
        {
            "status_code": status_code,
            "_json": json_data,
            "text": text,
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


@pytest.mark.parametrize(
    "url,expected",
    [
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
        (
            "https://gitlab.internal.example.com/group/subgroup/proj.git",
            "group/subgroup/proj",
        ),
        (
            "git@gitlab.internal.example.com:group/subgroup/proj.git",
            "group/subgroup/proj",
        ),
    ],
)
def test_parse_gitlab_project_path_valid(url, expected):
    assert _parse_gitlab_project_path(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not-a-url",
        "/absolute/path/to/repo.git",
        "git://invalid-protocol.example.com/ns/project.git",
    ],
)
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
    url = forge.open_merge_request(source_branch="feature/x", title="t", body="b")
    assert url == "https://gitlab.com/ns/project/-/merge_requests/1"
    assert captured["post_payload"]["source_branch"] == "feature/x"
    assert captured["post_payload"]["target_branch"] == "main"
    assert captured["post_payload"]["title"] == "t"
    assert captured["post_payload"]["description"] == "b"


def test_create_mr_409_falls_back_to_existing_mr(tmp_path, monkeypatch):
    """409 → find MR by source_branch and return its web_url."""
    project_json = {"id": 42}
    post_409 = _make_response(409, {}, "already exists")
    mr_list = [
        {
            "iid": 7,
            "web_url": "https://gitlab.com/ns/project/-/merge_requests/7",
            "state": "opened",
        }
    ]
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
    url = forge.open_merge_request(source_branch="feature/x", title="t", body="b")
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
        forge.open_merge_request(source_branch="feature/x", title="t", body="b")


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
        forge.open_merge_request(source_branch="feature/x", title="t", body="b")


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
        forge.open_merge_request(source_branch="feature/x", title="t", body="b")


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


@pytest.mark.parametrize(
    "merge_status,expected",
    [
        ("can_be_merged", True),
        ("cannot_be_merged", False),
        ("checking", None),
        ("unchecked", None),
    ],
)
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
# pr_status_by_url (URL-keyed fallback via _get_mr_by_iid)
# ---------------------------------------------------------------------------


def test_pr_status_by_url_resolves_merged_mr(tmp_path, monkeypatch):
    """A recorded MR web url resolves by IID to its current status."""
    project_json = {"id": 42}
    mr = {
        "iid": 7,
        "state": "merged",
        "web_url": "https://gitlab.com/ns/project/-/merge_requests/7",
        "merge_status": "can_be_merged",
        "sha": "abc123",
        "diff_refs": {"head_sha": "abc123"},
    }
    get_map = {
        "projects/42/merge_requests/7": _make_response(200, mr),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    status = forge.pr_status_by_url(
        url="https://gitlab.com/ns/project/-/merge_requests/7"
    )
    assert status == {
        "merged": True,
        "state": "merged",
        "url": "https://gitlab.com/ns/project/-/merge_requests/7",
        "mergeable": True,
        "sha": "abc123",
        "number": 7,
    }


def test_pr_status_by_url_unparseable_returns_none(tmp_path, monkeypatch):
    """A url without ``merge_requests/<n>`` → None (no API call)."""
    _mock_httpx(monkeypatch, get_map={})

    forge = _forge(tmp_path)
    assert forge.pr_status_by_url(url="https://gitlab.com/ns/project") is None


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
    mr = {
        "iid": 7,
        "state": "opened",
        "web_url": "http://x",
        "merge_status": "can_be_merged",
        "sha": "abc",
        "diff_refs": {"head_sha": "abc"},
    }
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
    mr = {
        "iid": 7,
        "state": "opened",
        "web_url": "http://x",
        "merge_status": "can_be_merged",
        "sha": "abc",
        "diff_refs": {"head_sha": "abc"},
    }
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
    mr = {
        "iid": 7,
        "state": "opened",
        "web_url": "http://x",
        "merge_status": "can_be_merged",
        "sha": "abc",
        "diff_refs": {"head_sha": "abc"},
    }
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
    mr = {
        "iid": 7,
        "state": "opened",
        "web_url": "http://x",
        "merge_status": "can_be_merged",
        "sha": "abc",
        "diff_refs": {"head_sha": "abc"},
    }
    get_map = {
        "merge_requests/7/pipelines": _make_response(200, []),
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.check_status(source_branch="feature/x")
    assert result == {"conclusion": None, "failing": []}


@pytest.mark.parametrize(
    "status",
    [
        "pending",
        "running",
        "created",
        "waiting_for_resource",
        "preparing",
        "manual",
        "scheduled",
    ],
)
def test_check_status_pipeline_pending_variants(tmp_path, monkeypatch, status):
    """All non-terminal statuses → conclusion=pending."""
    project_json = {"id": 42}
    mr = {
        "iid": 7,
        "state": "opened",
        "web_url": "http://x",
        "merge_status": "can_be_merged",
        "sha": "abc",
        "diff_refs": {"head_sha": "abc"},
    }
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
    mr = {
        "iid": 7,
        "state": "opened",
        "web_url": "http://x",
        "merge_status": "can_be_merged",
        "sha": "abc",
        "diff_refs": {"head_sha": "abc"},
    }
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
    mr = {
        "iid": 7,
        "state": "opened",
        "web_url": "http://x",
        "merge_status": "can_be_merged",
        "sha": "abc",
        "diff_refs": {"head_sha": "abc"},
    }
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
    mr = {
        "iid": 7,
        "state": "opened",
        "web_url": "http://x",
        "merge_status": "can_be_merged",
        "sha": "abc",
        "diff_refs": {"head_sha": "abc"},
    }
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
    mr = {
        "iid": 7,
        "state": "opened",
        "web_url": "http://x",
        "merge_status": "can_be_merged",
        "sha": "abc",
        "diff_refs": {"head_sha": "abc"},
    }
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
    assert result == {
        "merged": False,
        "reason": "merge not allowed (branch protection?)",
    }


def test_merge_pr_409_not_mergeable(tmp_path, monkeypatch):
    """409 → MR not mergeable."""
    project_json = {"id": 42}
    mr = {
        "iid": 7,
        "state": "opened",
        "web_url": "http://x",
        "merge_status": "can_be_merged",
        "sha": "abc",
        "diff_refs": {"head_sha": "abc"},
    }
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
    mr = {
        "iid": 7,
        "state": "opened",
        "web_url": "http://x",
        "merge_status": "can_be_merged",
        "sha": "abc",
        "diff_refs": {"head_sha": "abc"},
    }
    get_map = {
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }

    class ErrorClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

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
    mr = {
        "iid": 7,
        "state": "opened",
        "web_url": "http://x",
        "merge_status": "can_be_merged",
        "sha": "abc",
        "diff_refs": {"head_sha": "abc"},
    }
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
    mr = {
        "iid": 7,
        "state": "opened",
        "web_url": "http://x",
        "merge_status": "can_be_merged",
        "sha": "abc",
        "diff_refs": {"head_sha": "abc"},
    }
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


# ---------------------------------------------------------------------------
# _mr_changes
# ---------------------------------------------------------------------------


def test_mr_changes_happy_path(tmp_path, monkeypatch):
    """MR exists → _mr_changes returns normalized file dicts."""
    project_json = {"id": 42}
    mr = {"iid": 7, "web_url": "http://gl/ns/project/-/mr/7"}
    changes_resp = {
        "changes": [
            {
                "old_path": "src/main.py",
                "new_path": "src/main.py",
                "new_file": False,
                "deleted_file": False,
                "renamed_file": False,
                "diff": "--- a/src/main.py\n+++ b/src/main.py\n@@ -1,3 +1,5 @@\n-old\n+new1\n+new2\n old2",
            },
            {
                "old_path": "/dev/null",
                "new_path": "tests/test_main.py",
                "new_file": True,
                "deleted_file": False,
                "renamed_file": False,
                "diff": "--- /dev/null\n+++ b/tests/test_main.py\n@@ -0,0 +1,3 @@\n+line1\n+line2\n+line3",
            },
            {
                "old_path": "old/deprecated.py",
                "new_path": "/dev/null",
                "new_file": False,
                "deleted_file": True,
                "renamed_file": False,
                "diff": "--- a/old/deprecated.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-line1\n-line2",
            },
            {
                "old_path": "old_name.py",
                "new_path": "new_name.py",
                "new_file": False,
                "deleted_file": False,
                "renamed_file": True,
                "diff": "",
            },
        ]
    }
    get_map = {
        "merge_requests/7/changes": _make_response(200, changes_resp),
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
        "projects/42/merge_requests": _make_response(200, [mr]),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    files = forge._mr_changes(project_path="ns/project", mr_iid=7)
    assert len(files) == 4
    assert files[0] == {
        "path": "src/main.py",
        "status": "modified",
        "additions": 2,
        "deletions": 1,
    }
    assert files[1] == {
        "path": "tests/test_main.py",
        "status": "added",
        "additions": 3,
        "deletions": 0,
    }
    assert files[2] == {
        "path": "/dev/null",
        "status": "removed",
        "additions": 0,
        "deletions": 2,
    }
    assert files[3] == {
        "path": "new_name.py",
        "status": "renamed",
        "additions": 0,
        "deletions": 0,
    }


def test_mr_changes_no_mr(tmp_path, monkeypatch):
    """No MR for branch → pr_files returns []."""
    project_json = {"id": 42}
    get_map = {
        "merge_requests": _make_response(200, []),
        "projects/ns%2Fproject": _make_response(200, project_json),
        "projects/42/merge_requests": _make_response(200, []),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.pr_files(source_branch="no-such-branch")
    assert result == []


def test_mr_changes_http_error(tmp_path, monkeypatch):
    """HTTP error on changes endpoint → returns [] gracefully."""
    project_json = {"id": 42}
    mr = {"iid": 7, "web_url": "http://gl/ns/project/-/mr/7"}
    get_map = {
        "merge_requests/7/changes": _make_response(500, {}, "boom"),
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
        "projects/42/merge_requests": _make_response(200, [mr]),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    files = forge._mr_changes(project_path="ns/project", mr_iid=7)
    assert files == []


def test_mr_changes_no_diff(tmp_path, monkeypatch):
    """A change with no diff field → additions/deletions both 0."""
    project_json = {"id": 42}
    mr = {"iid": 7, "web_url": "http://gl/ns/project/-/mr/7"}
    changes_resp = {
        "changes": [
            {
                "old_path": "a.py",
                "new_path": "b.py",
                "new_file": False,
                "deleted_file": False,
                "renamed_file": False,
            },
        ]
    }
    get_map = {
        "merge_requests/7/changes": _make_response(200, changes_resp),
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
        "projects/42/merge_requests": _make_response(200, [mr]),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    files = forge._mr_changes(project_path="ns/project", mr_iid=7)
    assert len(files) == 1
    assert files[0]["path"] == "b.py"
    assert files[0]["additions"] == 0
    assert files[0]["deletions"] == 0


# ---------------------------------------------------------------------------
# list_pr_reviews
# ---------------------------------------------------------------------------


def test_list_pr_reviews_maps_general_notes(tmp_path, monkeypatch):
    """General (non-system, non-position) notes → mapped review dicts."""
    project_json = {"id": 42}
    mr = {"iid": 7, "web_url": "http://x"}
    notes = [
        {
            "id": 1,
            "system": False,
            "author": {"username": "alice"},
            "created_at": "2026-01-01T00:00:00Z",
            "body": "looks good",
        },
        # system note → dropped
        {"id": 2, "system": True, "author": {"username": "gitlab"}, "body": "merged"},
        # inline note (has position) → dropped, belongs to review_comments
        {
            "id": 3,
            "system": False,
            "author": {"username": "bob"},
            "body": "nit",
            "position": {"new_path": "a.py", "new_line": 5},
        },
        # note with null body → "" not None
        {"id": 4, "system": False, "author": {"username": "carol"}, "body": None},
    ]
    get_map = {
        "merge_requests/7/notes": _make_response(200, notes),
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    reviews = forge.list_pr_reviews(source_branch="feature/x")
    assert reviews == [
        {
            "id": 1,
            "author": "alice",
            "created_at": "2026-01-01T00:00:00Z",
            "body": "looks good",
        },
        {"id": 4, "author": "carol", "created_at": "", "body": ""},
    ]


def test_list_pr_reviews_no_mr_returns_empty(tmp_path, monkeypatch):
    """No MR → []."""
    project_json = {"id": 42}
    get_map = {
        "merge_requests": _make_response(200, []),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    assert forge.list_pr_reviews(source_branch="feature/x") == []


# ---------------------------------------------------------------------------
# list_review_comments
# ---------------------------------------------------------------------------


def test_list_review_comments_maps_inline_notes(tmp_path, monkeypatch):
    """Only notes with a position → inline comment dicts."""
    project_json = {"id": 42}
    mr = {"iid": 7, "web_url": "http://x"}
    notes = [
        # general note (no position) → dropped
        {"id": 1, "system": False, "author": {"username": "alice"}, "body": "hi"},
        {
            "id": 2,
            "system": False,
            "author": {"username": "bob"},
            "created_at": "2026-02-02T00:00:00Z",
            "body": "fix this",
            "position": {"new_path": "src/a.py", "new_line": 12},
        },
        # position without new_path → falls back to old_path; null new_line
        {
            "id": 3,
            "system": False,
            "author": {"username": "carol"},
            "body": None,
            "position": {"old_path": "src/b.py", "new_line": None},
        },
    ]
    get_map = {
        "merge_requests/7/notes": _make_response(200, notes),
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    comments = forge.list_review_comments(source_branch="feature/x")
    assert comments == [
        {
            "id": 2,
            "author": "bob",
            "created_at": "2026-02-02T00:00:00Z",
            "body": "fix this",
            "file_path": "src/a.py",
            "line": 12,
            "diff_hunk": "",
        },
        {
            "id": 3,
            "author": "carol",
            "created_at": "",
            "body": "",
            "file_path": "src/b.py",
            "line": None,
            "diff_hunk": "",
        },
    ]


def test_list_review_comments_no_mr_returns_empty(tmp_path, monkeypatch):
    """No MR → []."""
    project_json = {"id": 42}
    get_map = {
        "merge_requests": _make_response(200, []),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    assert forge.list_review_comments(source_branch="feature/x") == []


# ---------------------------------------------------------------------------
# pr_review_status
# ---------------------------------------------------------------------------


def test_pr_review_status_no_mr_returns_none(tmp_path, monkeypatch):
    """No MR → None (not the old PENDING stub)."""
    project_json = {"id": 42}
    get_map = {
        "merge_requests": _make_response(200, []),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    assert forge.pr_review_status(source_branch="feature/x") is None


def test_pr_review_status_approved(tmp_path, monkeypatch):
    """approvals.approved → state=APPROVED, comments + files populated."""
    project_json = {"id": 42}
    mr = {"iid": 7, "web_url": "http://x"}
    notes = [
        {"id": 1, "system": False, "author": {"username": "a"}, "body": "general"},
        {
            "id": 2,
            "system": False,
            "author": {"username": "b"},
            "body": "inline",
            "position": {"new_path": "src/a.py", "new_line": 3},
        },
        {"id": 3, "system": True, "body": "approved this MR"},
    ]
    changes_resp = {
        "changes": [
            {"old_path": "src/a.py", "new_path": "src/a.py", "diff": "+x"},
        ]
    }
    get_map = {
        "merge_requests/7/approvals": _make_response(200, {"approved": True}),
        "merge_requests/7/notes": _make_response(200, notes),
        "merge_requests/7/changes": _make_response(200, changes_resp),
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.pr_review_status(source_branch="feature/x")
    assert result["state"] == "APPROVED"
    assert result["files"] == ["src/a.py"]
    assert result["comments"] == [
        {"body": "general", "path": "", "line": None, "review_state": "APPROVED"},
        {
            "body": "inline",
            "path": "src/a.py",
            "line": 3,
            "review_state": "APPROVED",
        },
    ]


def test_pr_review_status_commented_when_notes_no_approval(tmp_path, monkeypatch):
    """Not approved but notes exist → state=COMMENTED."""
    project_json = {"id": 42}
    mr = {"iid": 7, "web_url": "http://x"}
    notes = [
        {"id": 1, "system": False, "author": {"username": "a"}, "body": "thoughts"},
    ]
    get_map = {
        "merge_requests/7/approvals": _make_response(200, {"approved": False}),
        "merge_requests/7/notes": _make_response(200, notes),
        "merge_requests/7/changes": _make_response(200, {"changes": []}),
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.pr_review_status(source_branch="feature/x")
    assert result["state"] == "COMMENTED"
    assert result["files"] == []


def test_pr_review_status_pending_when_no_notes(tmp_path, monkeypatch):
    """No approval and no non-system notes → state=PENDING."""
    project_json = {"id": 42}
    mr = {"iid": 7, "web_url": "http://x"}
    notes = [{"id": 1, "system": True, "body": "system event"}]
    get_map = {
        "merge_requests/7/approvals": _make_response(200, {"approved": False}),
        "merge_requests/7/notes": _make_response(200, notes),
        "merge_requests/7/changes": _make_response(200, {"changes": []}),
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.pr_review_status(source_branch="feature/x")
    assert result == {"state": "PENDING", "comments": [], "files": []}


# ---------------------------------------------------------------------------
# list_workflow_runs
# ---------------------------------------------------------------------------


def test_list_workflow_runs_maps_pipelines(tmp_path, monkeypatch):
    """Pipelines → mapped workflow-run dicts."""
    project_json = {"id": 42}
    pipelines = [
        {
            "id": 100,
            "ref": "feature/x",
            "sha": "abc123",
            "status": "success",
            "web_url": "http://gl/pipelines/100",
            "created_at": "2026-03-03T00:00:00Z",
        },
        {
            "id": 101,
            "ref": "feature/x",
            "sha": "def456",
            "status": "failed",
            "web_url": "http://gl/pipelines/101",
            "created_at": "2026-03-04T00:00:00Z",
        },
    ]
    get_map = {
        "projects/42/pipelines": _make_response(200, pipelines),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    runs = forge.list_workflow_runs(branch="feature/x", head_sha="abc123")
    assert runs == [
        {
            "id": 100,
            "name": "feature/x",
            "workflow_id": None,
            "head_sha": "abc123",
            "conclusion": "success",
            "html_url": "http://gl/pipelines/100",
            "created_at": "2026-03-03T00:00:00Z",
        },
        {
            "id": 101,
            "name": "feature/x",
            "workflow_id": None,
            "head_sha": "def456",
            "conclusion": "failure",
            "html_url": "http://gl/pipelines/101",
            "created_at": "2026-03-04T00:00:00Z",
        },
    ]


def test_list_workflow_runs_empty(tmp_path, monkeypatch):
    """No pipelines → []."""
    project_json = {"id": 42}
    get_map = {
        "projects/42/pipelines": _make_response(200, []),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    assert forge.list_workflow_runs() == []


# ---------------------------------------------------------------------------
# fetch_workflow_job_logs
# ---------------------------------------------------------------------------


def test_fetch_workflow_job_logs_concatenates_traces(tmp_path, monkeypatch):
    """Failed jobs → concatenated, ANSI-stripped, header-prefixed traces."""
    project_json = {"id": 42}
    failed_jobs = [
        {"id": 11, "name": "build"},
        {"id": 12, "name": "lint"},
    ]
    get_map = {
        "pipelines/100/jobs": _make_response(200, failed_jobs),
        "jobs/11/trace": _make_response(200, {}, "\x1b[31mboom\x1b[0m build error"),
        "jobs/12/trace": _make_response(200, {}, "lint failed"),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    logs = forge.fetch_workflow_job_logs(run_id=100)
    assert "### Job: build (id=11)" in logs
    assert "### Job: lint (id=12)" in logs
    # ANSI stripped
    assert "\x1b[31m" not in logs
    assert "boom build error" in logs
    assert "lint failed" in logs


def test_fetch_workflow_job_logs_no_failed_jobs(tmp_path, monkeypatch):
    """No failed jobs → ""."""
    project_json = {"id": 42}
    get_map = {
        "pipelines/100/jobs": _make_response(200, []),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    assert forge.fetch_workflow_job_logs(run_id=100) == ""


def test_fetch_workflow_job_logs_trace_fetch_failure_is_placeholder(
    tmp_path, monkeypatch
):
    """A failed trace fetch → placeholder, not a raise."""
    project_json = {"id": 42}
    failed_jobs = [{"id": 11, "name": "build"}]
    get_map = {
        "pipelines/100/jobs": _make_response(200, failed_jobs),
        "jobs/11/trace": _make_response(404, {}, "not found"),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    logs = forge.fetch_workflow_job_logs(run_id=100)
    assert "### Job: build (id=11)" in logs
    assert "[log fetch failed for job 11: HTTP 404]" in logs


# ---------------------------------------------------------------------------
# create_repo
# ---------------------------------------------------------------------------


def test_create_repo_disabled_raises_notconfigured(tmp_path, monkeypatch):
    """enable_repo_creation falsy → NotConfiguredError, no API call."""
    from robotsix_mill.forge.base import NotConfiguredError

    _mock_httpx(monkeypatch, get_map={})

    forge = _forge(tmp_path, enable_repo_creation=False)
    with pytest.raises(NotConfiguredError):
        forge.create_repo(name="proj", owner="ns", private=True, description="d")


def test_create_repo_201_returns_repo_info(tmp_path, monkeypatch):
    """201 → populated RepoInfo; namespace resolved from owner."""
    created = {
        "id": 555,
        "path": "proj",
        "name": "Proj",
        "http_url_to_repo": "https://gitlab.com/ns/proj.git",
        "web_url": "https://gitlab.com/ns/proj",
    }
    get_map = {
        "namespaces/ns": _make_response(200, {"id": 9}),
    }
    captured = _mock_httpx(
        monkeypatch,
        get_map=get_map,
        post_response=_make_response(201, created),
    )

    forge = _forge(tmp_path, enable_repo_creation=True)
    info = forge.create_repo(name="proj", owner="ns", private=True, description="d")
    assert info.id == 555
    assert info.name == "proj"
    assert info.clone_url == "https://gitlab.com/ns/proj.git"
    assert info.html_url == "https://gitlab.com/ns/proj"
    assert captured["post_payload"] == {
        "name": "proj",
        "visibility": "private",
        "description": "d",
        "namespace_id": 9,
    }


def test_create_repo_public_no_owner(tmp_path, monkeypatch):
    """private=False, empty owner → visibility=public, no namespace_id."""
    created = {
        "id": 1,
        "path": "proj",
        "name": "proj",
        "http_url_to_repo": "https://gitlab.com/me/proj.git",
        "web_url": "https://gitlab.com/me/proj",
    }
    captured = _mock_httpx(
        monkeypatch,
        get_map={},
        post_response=_make_response(201, created),
    )

    forge = _forge(tmp_path, enable_repo_creation=True)
    info = forge.create_repo(name="proj", owner="", private=False, description="d")
    assert info.id == 1
    assert captured["post_payload"] == {
        "name": "proj",
        "visibility": "public",
        "description": "d",
    }


def test_create_repo_name_conflict_raises_runtimeerror(tmp_path, monkeypatch):
    """400 name-taken → RuntimeError describing the conflict."""
    get_map = {
        "namespaces/ns": _make_response(200, {"id": 9}),
    }
    _mock_httpx(
        monkeypatch,
        get_map=get_map,
        post_response=_make_response(400, {}, "{'name': ['has already been taken']}"),
    )

    forge = _forge(tmp_path, enable_repo_creation=True)
    with pytest.raises(RuntimeError, match="already exists"):
        forge.create_repo(name="proj", owner="ns", private=True, description="d")


def test_create_repo_other_error_raises_runtimeerror(tmp_path, monkeypatch):
    """Non-201, non-conflict → generic RuntimeError."""
    get_map = {
        "namespaces/ns": _make_response(200, {"id": 9}),
    }
    _mock_httpx(
        monkeypatch,
        get_map=get_map,
        post_response=_make_response(403, {}, "forbidden"),
    )

    forge = _forge(tmp_path, enable_repo_creation=True)
    with pytest.raises(RuntimeError, match="GitLab repo create failed"):
        forge.create_repo(name="proj", owner="ns", private=True, description="d")


# ---------------------------------------------------------------------------
# _delete_branch (via delete_branch)
# ---------------------------------------------------------------------------


def _mock_httpx_delete(monkeypatch, *, delete_response=None, raise_exc=None):
    """Replace httpx.Client with a mock that resolves the project id (GET)
    and exposes a controllable .delete()."""
    captured = {"url": None}

    class MockClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, headers=None, params=None, **kwargs):
            # project-id resolution
            return _make_response(200, {"id": 7})

        def delete(self, url, headers=None, **kwargs):
            captured["url"] = url
            if raise_exc is not None:
                raise raise_exc
            return delete_response

    monkeypatch.setattr(real_httpx, "Client", MockClient)
    return captured


def test_delete_branch_204_returns_true(tmp_path, monkeypatch):
    cap = _mock_httpx_delete(monkeypatch, delete_response=_make_response(204, {}))
    forge = _forge(tmp_path)
    assert forge.delete_branch(branch="mill/t-1") is True
    assert cap["url"].endswith("/projects/7/repository/branches/mill%2Ft-1")


def test_delete_branch_404_returns_true(tmp_path, monkeypatch):
    _mock_httpx_delete(monkeypatch, delete_response=_make_response(404, {}, "gone"))
    forge = _forge(tmp_path)
    assert forge.delete_branch(branch="mill/t-1") is True


def test_delete_branch_other_status_returns_false(tmp_path, monkeypatch):
    _mock_httpx_delete(monkeypatch, delete_response=_make_response(500, {}, "boom"))
    forge = _forge(tmp_path)
    assert forge.delete_branch(branch="mill/t-1") is False


def test_delete_branch_exception_returns_false(tmp_path, monkeypatch):
    _mock_httpx_delete(monkeypatch, raise_exc=real_httpx.ConnectError("net down"))
    forge = _forge(tmp_path)
    assert forge.delete_branch(branch="mill/t-1") is False
