"""Test GitLabForge HTTP seams with mocked httpx.Client.

No stage-level monkeypatching — tests call _create_mr, _find_mr, _get_latest_pipeline,
_parse_gitlab_project_path, and _build_headers directly with a mocked transport.
"""

import httpx as real_httpx
import pytest

from robotsix_mill.config import Secrets, Settings, _reset_secrets
from robotsix_mill.scm.forge.gitlab import (
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
    kw.setdefault("forge_kind", "gitlab")
    kw.setdefault("forge_remote_url", "https://gitlab.com/ns/project.git")
    kw.setdefault("FORGE_TOKEN", "glpat-token")
    # Mirror forge_token into Secrets so get_secrets() works
    ft = kw.get("FORGE_TOKEN")
    if ft is not None:
        _set_secrets(forge_token=ft)
    # FORGE_TOKEN is now a Secrets-only field; pop before Settings()
    kw.pop("FORGE_TOKEN", None)
    s = Settings(**kw)
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
    captured = {"post_payload": None, "post_url": None, "put_payload": None}

    class MockClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, headers=None, json=None, **kwargs):
            captured["post_payload"] = json
            captured["post_url"] = url
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
    ("url", "expected"),
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


def test_create_mr_target_honors_repo_working_branch(tmp_path, monkeypatch):
    """A repo_config.working_branch must become the MR target_branch
    (same seam as the GitHub 422 base-invalid regression on 5a2a)."""
    from robotsix_mill.config import RepoConfig

    project_json = {"id": 42}
    mr_json = {"web_url": "https://gitlab.com/ns/project/-/merge_requests/2"}
    get_map = {"projects/ns%2Fproject": _make_response(200, project_json)}
    captured = _mock_httpx(
        monkeypatch,
        get_map=get_map,
        post_response=_make_response(201, mr_json),
    )

    rc = RepoConfig(
        repo_id="r",
        board_id="b",
        langfuse_project_name="r",
        langfuse_public_key="",
        langfuse_secret_key="",
        working_branch="lyrical",
    )
    forge = GitLabForge(_settings(tmp_path), repo_config=rc)
    forge.open_merge_request(source_branch="feature/x", title="t", body="b")
    assert captured["post_payload"]["target_branch"] == "lyrical"


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


def test_create_cross_project_mr_201_returns_web_url(tmp_path, monkeypatch):
    """When head_repo is set, create the MR on the fork project with
    target_project_id pointing to the upstream project."""
    from robotsix_mill.config import CrossRepoTarget, RepoConfig

    upstream_json = {"id": 42}
    fork_json = {"id": 99}
    mr_json = {
        "web_url": "https://gitlab.com/fork-ns/fork/-/merge_requests/3",
    }
    get_map = {
        "projects/fork-ns%2Ffork": _make_response(200, fork_json),
        "projects/ns%2Fproject": _make_response(200, upstream_json),
    }
    captured = _mock_httpx(
        monkeypatch,
        get_map=get_map,
        post_response=_make_response(201, mr_json),
    )

    rc = RepoConfig(
        repo_id="r",
        board_id="b",
        langfuse_project_name="r",
        langfuse_public_key="",
        langfuse_secret_key="",
        cross_repo_target=CrossRepoTarget(
            base_branch="develop",
            upstream_remote_url="https://gitlab.com/ns/project.git",
            fork_remote_url="https://gitlab.com/fork-ns/fork.git",
        ),
    )
    forge = GitLabForge(_settings(tmp_path), repo_config=rc)
    url = forge.open_merge_request(
        source_branch="feature/x",
        title="t",
        body="b",
        head_repo="fork-ns/fork",
    )
    assert url == "https://gitlab.com/fork-ns/fork/-/merge_requests/3"
    # MR was created on the fork project (id 99) targeting upstream (id 42)
    assert captured["post_payload"]["source_branch"] == "feature/x"
    assert captured["post_payload"]["target_branch"] == "develop"
    assert captured["post_payload"]["target_project_id"] == 42
    assert captured["post_payload"]["title"] == "t"
    assert captured["post_payload"]["description"] == "b"


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
        "author": "",
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
    ("merge_status", "expected"),
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
        "author": "",
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
    assert result == {"conclusion": "success", "failing": [], "pending": [], "jobs": []}


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
        {"id": 200, "name": "build", "stage": "test"},
        {"id": 201, "name": "lint", "stage": "test"},
    ]
    get_map = {
        "merge_requests/7/pipelines": _make_response(200, [pipeline]),
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
        "pipelines/100/jobs": _make_response(200, failed_jobs),
        "jobs/200/trace": _make_response(
            200, None, "\x1b[31mbuild failed: boom\x1b[0m"
        ),
        "jobs/201/trace": _make_response(200, None, "lint failed: nope"),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.check_status(source_branch="feature/x")
    assert result["conclusion"] == "failure"
    assert len(result["failing"]) == 2
    assert result["failing"][0]["name"] == "build"
    assert result["failing"][0]["annotations"] == []
    # Trace is fetched, ANSI-stripped, and surfaced as summary/text detail.
    assert result["failing"][0]["summary"] == "build failed: boom"
    assert result["failing"][0]["text"] == "build failed: boom"
    assert result["failing"][1]["summary"] == "lint failed: nope"


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
    assert result == {"conclusion": "pending", "failing": [], "pending": [], "jobs": []}


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
    assert result == {"conclusion": None, "failing": [], "pending": [], "jobs": []}


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
        "canceled",
        "skipped",
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


def test_check_status_pipeline_canceled_is_pending(tmp_path, monkeypatch):
    """Pipeline status=canceled → conclusion=pending (like GitHub's
    _INCONCLUSIVE_CONCLUSIONS, avoids false CI-failure loops)."""
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
    assert result["conclusion"] == "pending"


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
    assert result == {"merged": True, "reason": "merged", "merge_commit_sha": "def456"}
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
    """405 → retryable, carrying the forge's own message."""
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
        "retryable": True,
        "reason": "merge not allowed: not allowed",
    }


def test_merge_pr_409_not_mergeable(tmp_path, monkeypatch):
    """409 → retryable, carrying the forge's own message."""
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
    assert result == {
        "merged": False,
        "retryable": True,
        "reason": "MR is not mergeable: conflict",
    }


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
# update_branch (MR rebase)
# ---------------------------------------------------------------------------


def test_update_branch_mr_not_found(tmp_path, monkeypatch):
    """No MR → {"updated": False, "reason": "MR not found"}."""
    project_json = {"id": 42}
    get_map = {
        "merge_requests": _make_response(200, []),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.update_branch(source_branch="feature/x")
    assert result == {"updated": False, "reason": "MR not found"}


def test_update_branch_202_accepted(tmp_path, monkeypatch):
    """202 → {"updated": True, "reason": "rebase accepted"}."""
    project_json = {"id": 42}
    mr = {"iid": 7, "state": "opened", "web_url": "http://x"}
    get_map = {
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(
        monkeypatch,
        get_map=get_map,
        put_response=_make_response(202, {"rebase_in_progress": True}),
    )

    forge = _forge(tmp_path)
    result = forge.update_branch(source_branch="feature/x")
    assert result == {"updated": True, "reason": "rebase accepted"}


def test_update_branch_403_forbidden(tmp_path, monkeypatch):
    """403 → not updated, permissions reason."""
    project_json = {"id": 42}
    mr = {"iid": 7, "state": "opened", "web_url": "http://x"}
    get_map = {
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(
        monkeypatch,
        get_map=get_map,
        put_response=_make_response(403, {}, "forbidden"),
    )

    forge = _forge(tmp_path)
    result = forge.update_branch(source_branch="feature/x")
    assert result == {
        "updated": False,
        "reason": "rebase forbidden (insufficient permissions?)",
    }


def test_update_branch_409_not_mergeable(tmp_path, monkeypatch):
    """409 → MR is not mergeable."""
    project_json = {"id": 42}
    mr = {"iid": 7, "state": "opened", "web_url": "http://x"}
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
    result = forge.update_branch(source_branch="feature/x")
    assert result == {"updated": False, "reason": "MR is not mergeable"}


def test_update_branch_unexpected_status(tmp_path, monkeypatch):
    """Unexpected HTTP status → {"updated": False, ...} with status text."""
    project_json = {"id": 42}
    mr = {"iid": 7, "state": "opened", "web_url": "http://x"}
    get_map = {
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(
        monkeypatch,
        get_map=get_map,
        put_response=_make_response(500, {}, "boom"),
    )

    forge = _forge(tmp_path)
    result = forge.update_branch(source_branch="feature/x")
    assert result["updated"] is False
    assert "HTTP 500" in result["reason"]


def test_update_branch_find_mr_raises_returns_graceful_dict(tmp_path, monkeypatch):
    """When project lookup fails, update_branch returns a dict, never raises."""
    get_map = {
        "projects/ns%2Fproject": _make_response(500, {}, "internal error"),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.update_branch(source_branch="feature/x")
    assert result["updated"] is False
    assert "GitLab project lookup failed" in result["reason"]


def test_update_branch_network_error(tmp_path, monkeypatch):
    """Network error during PUT → {"updated": False} (no raise)."""
    project_json = {"id": 42}
    mr = {"iid": 7, "state": "opened", "web_url": "http://x"}
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
    result = forge.update_branch(source_branch="feature/x")
    assert result["updated"] is False
    assert "connection refused" in result["reason"]


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
# close_pr
# ---------------------------------------------------------------------------


def test_close_pr_success(tmp_path, monkeypatch):
    """Mock returns 200 → True."""
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
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    monkeypatch.setattr(forge, "_close_mr", lambda project_path, mr_iid: True)
    result = forge.close_pr(source_branch="feature/x")
    assert result is True


def test_close_pr_not_found(tmp_path, monkeypatch):
    """_find_mr returns None → False, no HTTP call made."""
    project_json = {"id": 42}
    get_map = {
        "merge_requests": _make_response(200, []),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.close_pr(source_branch="feature/x")
    assert result is False


def test_close_pr_already_closed(tmp_path, monkeypatch):
    """Mock returns False (e.g. 405/422 already-closed) → False, no exception."""
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
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    monkeypatch.setattr(forge, "_close_mr", lambda project_path, mr_iid: False)
    result = forge.close_pr(source_branch="feature/x")
    assert result is False


# ---------------------------------------------------------------------------
# post_pr_comment
# ---------------------------------------------------------------------------


def test_post_pr_comment_success(tmp_path, monkeypatch):
    """Mock returns 201 → True."""
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
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    monkeypatch.setattr(forge, "_post_mr_note", lambda project_path, mr_iid, body: True)
    result = forge.post_pr_comment(source_branch="feature/x", body="closing note")
    assert result is True


def test_post_pr_comment_not_found(tmp_path, monkeypatch):
    """_find_mr returns None → False."""
    project_json = {"id": 42}
    get_map = {
        "merge_requests": _make_response(200, []),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.post_pr_comment(source_branch="feature/x", body="closing note")
    assert result is False


def test_post_pr_comment_error(tmp_path, monkeypatch):
    """Mock raises → False, no exception propagated."""
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
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    monkeypatch.setattr(
        forge,
        "_post_mr_note",
        lambda project_path, mr_iid, body: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    result = forge.post_pr_comment(source_branch="feature/x", body="closing note")
    assert result is False


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
# list_pr_comments
# ---------------------------------------------------------------------------


def test_list_pr_comments_maps_non_system_notes(tmp_path, monkeypatch):
    """Non-system notes → conversation comment dicts; system notes dropped."""
    project_json = {"id": 42}
    mr = {"iid": 7, "web_url": "http://x"}
    notes = [
        {
            "id": 1,
            "system": False,
            "author": {"username": "alice"},
            "created_at": "2026-01-01T00:00:00Z",
            "body": "disposition: merged",
        },
        # system note (state change / label edit) → dropped
        {"id": 2, "system": True, "author": {"username": "gitlab"}, "body": "merged"},
        # inline note (has position) is still a conversation comment here —
        # list_pr_comments filters only on `system`, mirroring GitHub.
        {
            "id": 3,
            "system": False,
            "author": {"username": "bob"},
            "created_at": "2026-01-02T00:00:00Z",
            "body": "nit",
            "position": {"new_path": "a.py", "new_line": 5},
        },
        # note with null body → "" not None; missing author → ""
        {"id": 4, "system": False, "author": None, "body": None},
    ]
    get_map = {
        "merge_requests/7/notes": _make_response(200, notes),
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    comments = forge.list_pr_comments(source_branch="feature/x")
    assert comments == [
        {
            "id": 1,
            "author": "alice",
            "created_at": "2026-01-01T00:00:00Z",
            "body": "disposition: merged",
        },
        {
            "id": 3,
            "author": "bob",
            "created_at": "2026-01-02T00:00:00Z",
            "body": "nit",
        },
        {"id": 4, "author": "", "created_at": "", "body": ""},
    ]


def test_list_pr_comments_no_mr_returns_empty(tmp_path, monkeypatch):
    """No MR → []."""
    project_json = {"id": 42}
    get_map = {
        "merge_requests": _make_response(200, []),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    assert forge.list_pr_comments(source_branch="feature/x") == []


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


def test_pr_review_status_changes_requested_unresolved_thread(tmp_path, monkeypatch):
    """An unresolved resolvable note → CHANGES_REQUESTED, even if approved."""
    project_json = {"id": 42}
    mr = {"iid": 7, "web_url": "http://x"}
    notes = [
        {
            "id": 1,
            "system": False,
            "author": {"username": "a"},
            "body": "please fix",
            "resolvable": True,
            "resolved": False,
        },
    ]
    get_map = {
        "merge_requests/7/approvals": _make_response(200, {"approved": True}),
        "merge_requests/7/notes": _make_response(200, notes),
        "merge_requests/7/changes": _make_response(200, {"changes": []}),
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.pr_review_status(source_branch="feature/x")
    # Unresolved blocking thread takes precedence over approval.
    assert result["state"] == "CHANGES_REQUESTED"


def test_pr_review_status_dismissed_when_approval_revoked(tmp_path, monkeypatch):
    """No approval, no unresolved notes, but a revoked-approval system note
    → DISMISSED."""
    project_json = {"id": 42}
    mr = {"iid": 7, "web_url": "http://x"}
    notes = [{"id": 1, "system": True, "body": "unapproved this merge request"}]
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
    assert result["state"] == "DISMISSED"


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
        # Non-terminal pipeline — should be excluded by client-side filter.
        {
            "id": 999,
            "ref": "feature/x",
            "sha": "running-sha",
            "status": "running",
            "web_url": "http://gl/pipelines/999",
            "created_at": "2026-03-05T00:00:00Z",
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
            "run_attempt": None,
            "html_url": "http://gl/pipelines/100",
            "created_at": "2026-03-03T00:00:00Z",
            "path": "",
            "head_branch": "feature/x",
        },
        {
            "id": 101,
            "name": "feature/x",
            "workflow_id": None,
            "head_sha": "def456",
            "conclusion": "failure",
            "run_attempt": None,
            "html_url": "http://gl/pipelines/101",
            "created_at": "2026-03-04T00:00:00Z",
            "path": "",
            "head_branch": "feature/x",
        },
    ]


def test_list_workflow_runs_canceled_skipped(tmp_path, monkeypatch):
    """Canceled/skipped pipelines map to 'cancelled'/'skipped' (not 'pending')."""
    project_json = {"id": 42}
    pipelines = [
        {
            "id": 200,
            "ref": "feature/y",
            "sha": "ccc111",
            "status": "canceled",
            "web_url": "http://gl/pipelines/200",
            "created_at": "2026-03-05T00:00:00Z",
        },
        {
            "id": 201,
            "ref": "feature/y",
            "sha": "ddd222",
            "status": "skipped",
            "web_url": "http://gl/pipelines/201",
            "created_at": "2026-03-06T00:00:00Z",
        },
    ]
    get_map = {
        "projects/42/pipelines": _make_response(200, pipelines),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    runs = forge.list_workflow_runs()
    assert runs == [
        {
            "id": 200,
            "name": "feature/y",
            "workflow_id": None,
            "head_sha": "ccc111",
            "conclusion": "cancelled",
            "run_attempt": None,
            "html_url": "http://gl/pipelines/200",
            "created_at": "2026-03-05T00:00:00Z",
            "path": "",
            "head_branch": "feature/y",
        },
        {
            "id": 201,
            "name": "feature/y",
            "workflow_id": None,
            "head_sha": "ddd222",
            "conclusion": "skipped",
            "run_attempt": None,
            "html_url": "http://gl/pipelines/201",
            "created_at": "2026-03-06T00:00:00Z",
            "path": "",
            "head_branch": "feature/y",
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


def test_list_tag_workflow_runs_filters_on_tag_glob(tmp_path):
    """Tag pipelines (scope=tags) are filtered on their ref via fnmatch."""
    pipelines = [
        {
            "id": 301,
            "ref": "v1.2.0",
            "sha": "t1",
            "status": "failed",
            "web_url": "http://gl/pipelines/301",
            "created_at": "2026-03-08T00:00:00Z",
        },
        {
            "id": 300,
            "ref": "nightly-1",
            "sha": "t0",
            "status": "success",
            "web_url": "http://gl/pipelines/300",
            "created_at": "2026-03-07T00:00:00Z",
        },
        {
            "id": 302,
            "ref": "v1.3.0",
            "sha": "t2",
            "status": "running",
            "web_url": "http://gl/pipelines/302",
            "created_at": "2026-03-09T00:00:00Z",
        },
    ]
    calls = []

    class _FakeHttp:
        def get(self, path, **kw):
            calls.append((path, kw.get("params")))
            return _make_response(200, pipelines)

    forge = _forge(tmp_path)
    forge._http = _FakeHttp()
    forge._resolve_project_id = lambda _path: 42

    runs = forge.list_tag_workflow_runs(tag_glob="v*")

    assert calls == [("/projects/42/pipelines", {"per_page": 30, "scope": "tags"})]
    assert [r["id"] for r in runs] == [301]
    assert runs[0]["head_branch"] == "v1.2.0"
    assert runs[0]["conclusion"] == "failure"
    assert runs[0]["head_sha"] == "t1"


def test_list_tag_workflow_runs_no_tagged_runs(tmp_path):
    """No tag pipelines, or none matching the glob → []."""

    class _FakeHttp:
        def __init__(self, payload):
            self.payload = payload

        def get(self, path, **kw):
            return _make_response(200, self.payload)

    forge = _forge(tmp_path)
    forge._resolve_project_id = lambda _path: 42
    forge._http = _FakeHttp([])
    assert forge.list_tag_workflow_runs(tag_glob="v*") == []

    forge._http = _FakeHttp(
        [
            {
                "id": 1,
                "ref": "nightly-1",
                "sha": "t0",
                "status": "success",
                "web_url": "http://gl/pipelines/1",
                "created_at": "2026-03-07T00:00:00Z",
            }
        ]
    )
    assert forge.list_tag_workflow_runs(tag_glob="v*") == []


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
# rerun_workflow
# ---------------------------------------------------------------------------


def test_rerun_workflow_201_returns_rerun_true(tmp_path, monkeypatch):
    """POST /projects/:id/pipelines/:run_id/retry → 201 → {"rerun": True}."""
    project_json = {"id": 42}
    get_map = {"projects/ns%2Fproject": _make_response(200, project_json)}
    captured = _mock_httpx(
        monkeypatch,
        get_map=get_map,
        post_response=_make_response(201, {"id": 100}),
    )

    forge = _forge(tmp_path)
    result = forge.rerun_workflow(run_id=100)
    assert result == {"rerun": True}
    assert "/projects/42/pipelines/100/retry" in captured["post_url"]


def test_rerun_workflow_non_201_returns_rerun_false(tmp_path, monkeypatch):
    """A non-201 response → {"rerun": False, "reason": ...}."""
    project_json = {"id": 42}
    get_map = {"projects/ns%2Fproject": _make_response(200, project_json)}
    _mock_httpx(
        monkeypatch,
        get_map=get_map,
        post_response=_make_response(403, {"message": "forbidden"}),
    )

    forge = _forge(tmp_path)
    result = forge.rerun_workflow(run_id=100)
    assert result["rerun"] is False
    assert "HTTP 403" in result["reason"]


def test_rerun_workflow_resolve_project_failure_returns_rerun_false(
    tmp_path, monkeypatch
):
    """When project resolution fails, the public method catches it."""
    # No get_map entry for the project → 404 → RuntimeError from _resolve_project_id.
    _mock_httpx(monkeypatch, get_map={})

    forge = _forge(tmp_path)
    result = forge.rerun_workflow(run_id=100)
    assert result["rerun"] is False
    assert result["reason"]  # non-empty reason


def test_rerun_workflow_post_exception_returns_rerun_false(tmp_path, monkeypatch):
    """When the POST raises (e.g. network error), return failure dict."""
    project_json = {"id": 42}
    get_map = {"projects/ns%2Fproject": _make_response(200, project_json)}

    class FailingPostResponse:
        status_code = 500

        def raise_for_status(self):
            raise real_httpx.HTTPStatusError(
                "server error",
                request=real_httpx.Request("POST", "http://x"),
                response=self,
            )

        def json(self):
            return {}

    _mock_httpx(
        monkeypatch,
        get_map=get_map,
        post_response=FailingPostResponse(),
    )

    forge = _forge(tmp_path)
    result = forge.rerun_workflow(run_id=100)
    assert result["rerun"] is False
    assert result["reason"]


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


# ---------------------------------------------------------------------------
# _list_branches / _list_open_pr_branches
# ---------------------------------------------------------------------------


def _gl_branch(name, date, protected=False):
    return {
        "name": name,
        "protected": protected,
        "commit": {"committed_date": date},
    }


def _mock_httpx_list(monkeypatch, *, branch_url, pages=None, raise_on_list=False):
    """Mock httpx.Client: resolves project id (GET on the project path) and
    serves paginated list responses (GET whose url contains *branch_url*)."""

    class MockClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, headers=None, params=None, **kwargs):
            if branch_url in url:
                if raise_on_list:
                    raise real_httpx.ConnectError("net down")
                page = (params or {}).get("page", 1)
                return pages[page - 1]
            # project-id resolution
            return _make_response(200, {"id": 7})

    monkeypatch.setattr(real_httpx, "Client", MockClient)


def test_list_branches_parses_and_paginates(tmp_path, monkeypatch):
    page1 = [
        _gl_branch(f"b{i}", "2024-01-15T10:30:00.000+00:00", protected=(i == 0))
        for i in range(100)
    ]
    page2 = [_gl_branch("last", "2024-02-01T08:00:00.000Z", protected=True)]
    _mock_httpx_list(
        monkeypatch,
        branch_url="/repository/branches",
        pages=[_make_response(200, page1), _make_response(200, page2)],
    )
    forge = _forge(tmp_path)
    branches = forge.list_branches()
    assert len(branches) == 101
    assert branches[0].name == "b0"
    assert branches[0].is_protected is True
    assert branches[1].is_protected is False
    assert branches[0].last_commit_at.tzinfo is not None
    assert branches[0].last_commit_at.utcoffset().total_seconds() == 0
    assert branches[-1].name == "last"


def test_list_branches_exception_returns_empty(tmp_path, monkeypatch):
    _mock_httpx_list(monkeypatch, branch_url="/repository/branches", raise_on_list=True)
    forge = _forge(tmp_path)
    assert forge.list_branches() == []


def test_list_branches_non_2xx_returns_empty(tmp_path, monkeypatch):
    _mock_httpx_list(
        monkeypatch,
        branch_url="/repository/branches",
        pages=[_make_response(500, [], "boom")],
    )
    forge = _forge(tmp_path)
    assert forge.list_branches() == []


def test_list_open_pr_branches_returns_source_branches(tmp_path, monkeypatch):
    mrs = [
        {"source_branch": "feature/a"},
        {"source_branch": "feature/b"},
        {"source_branch": ""},
    ]
    _mock_httpx_list(
        monkeypatch,
        branch_url="/merge_requests",
        pages=[_make_response(200, mrs)],
    )
    forge = _forge(tmp_path)
    assert forge.list_open_pr_branches() == {"feature/a", "feature/b"}


def test_list_open_pr_branches_exception_returns_empty(tmp_path, monkeypatch):
    _mock_httpx_list(monkeypatch, branch_url="/merge_requests", raise_on_list=True)
    forge = _forge(tmp_path)
    assert forge.list_open_pr_branches() == set()


# ---------------------------------------------------------------------------
# _capture_failure_window with GitLab-specific regex
# ---------------------------------------------------------------------------


def test_capture_failure_window_gitlab_anchors_on_error():
    """GitLab-style failure markers (^ERROR:, Job failed) trigger anchoring."""
    from robotsix_mill.scm.forge._log_utils import _capture_failure_window
    from robotsix_mill.scm.forge.gitlab import _LOG_FAILURE_RE

    real = "ERROR: failed to build proxy image: COPY filter not found\n"
    filler = "noise line padding the log\n" * 5000
    mask = "Job failed: exit code 1\n"
    log = real + filler + mask
    out = _capture_failure_window(log, max_bytes=4000, failure_re=_LOG_FAILURE_RE)
    assert "failed to build proxy image" in out
    assert "anchored on first failure marker" in out
    assert len(out) <= 4000 + 100


def test_capture_failure_window_gitlab_fatal_marker():
    """GitLab regex matches 'fatal:' patterns."""
    from robotsix_mill.scm.forge._log_utils import _capture_failure_window
    from robotsix_mill.scm.forge.gitlab import _LOG_FAILURE_RE

    # Put the fatal marker early enough that it falls outside the tail window
    # when max_bytes is small relative to total log length.
    log = "fatal: unable to access 'https://gitlab.com/...'\n" + ("noise\n" * 5000)
    out = _capture_failure_window(log, max_bytes=2000, failure_re=_LOG_FAILURE_RE)
    assert "fatal:" in out
    assert "anchored on first failure marker" in out


def test_capture_failure_window_gitlab_tailcaps_without_marker():
    """No GitLab failure marker → degrade to tail-cap."""
    from robotsix_mill.scm.forge._log_utils import _capture_failure_window
    from robotsix_mill.scm.forge.gitlab import _LOG_FAILURE_RE

    out = _capture_failure_window(
        "x" * 100_000, max_bytes=65536, failure_re=_LOG_FAILURE_RE
    )
    assert out == "x" * 65536


# ---------------------------------------------------------------------------
# required_status_contexts — explicit no-op mirroring GitHub's return contract
# ---------------------------------------------------------------------------


def test_required_status_contexts_returns_empty_list_matching_github_contract(
    tmp_path,
):
    """GitLab has no per-context branch protection, so the override returns
    [] (the ABC's "unknown" contract) — the same shape GitHub's adapter
    returns for a 403/404 (unprotected/unreadable) branch.  Must NEVER
    raise, and must return only str elements.
    """
    forge = _forge(tmp_path)
    result = forge.required_status_contexts(target_branch="main")
    assert isinstance(result, list)
    assert result == []
    assert all(isinstance(c, str) for c in result)
    # No API reachable / no protection configured → still [] for any target.
    assert forge.required_status_contexts(target_branch="release/1.0") == []


# ---------------------------------------------------------------------------
# get_workflow_run — single-pipeline lookup (ci-auto-close anchor recovery)
# ---------------------------------------------------------------------------


def test_get_workflow_run_maps_pipeline(tmp_path, monkeypatch):
    """GET /projects/:id/pipelines/:id → canonical run dict."""
    project_json = {"id": 42}
    pipeline = {
        "id": 100,
        "ref": "feature/x",
        "sha": "abc123",
        "status": "failed",
        "web_url": "http://gl/pipelines/100",
        "created_at": "2026-03-03T00:00:00Z",
    }
    get_map = {
        "pipelines/100": _make_response(200, pipeline),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    run = forge.get_workflow_run(run_id=100)
    assert run == {
        "id": 100,
        "name": "feature/x",
        "path": "",
        "head_branch": "feature/x",
        "head_sha": "abc123",
        "conclusion": "failure",
        "run_attempt": None,
        "html_url": "http://gl/pipelines/100",
        "created_at": "2026-03-03T00:00:00Z",
    }


def test_get_workflow_run_error_returns_none(tmp_path, monkeypatch):
    """Any error (missing pipeline / transport) → None, never raises."""
    project_json = {"id": 42}
    get_map = {
        "pipelines/100": _make_response(404, {}, "not found"),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    assert forge.get_workflow_run(run_id=100) is None


# ---------------------------------------------------------------------------
# list_workflow_run_jobs — pipeline jobs (cancelled-pipeline redispatch guard)
# ---------------------------------------------------------------------------


def test_list_workflow_run_jobs_maps_jobs(tmp_path, monkeypatch):
    """GET /projects/:id/pipelines/:id/jobs → id/name/status/conclusion dicts."""
    project_json = {"id": 42}
    jobs = [
        {"id": 11, "name": "build", "status": "failed"},
        {"id": 12, "name": "test", "status": "success"},
    ]
    get_map = {
        "pipelines/100/jobs": _make_response(200, jobs),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.list_workflow_run_jobs(run_id=100)
    assert result == [
        {"id": 11, "name": "build", "status": "failed", "conclusion": "failure"},
        {"id": 12, "name": "test", "status": "success", "conclusion": "success"},
    ]


def test_list_workflow_run_jobs_empty_when_no_jobs(tmp_path, monkeypatch):
    """A pipeline that started no job → [] (the queue-timeout signature)."""
    project_json = {"id": 42}
    get_map = {
        "pipelines/100/jobs": _make_response(200, []),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    assert forge.list_workflow_run_jobs(run_id=100) == []


def test_list_workflow_run_jobs_raises_on_transport_error(tmp_path, monkeypatch):
    """Transport error propagates so the caller can tell 'no jobs' from
    'could not tell' (mirrors the GitHub seam)."""
    project_json = {"id": 42}
    get_map = {
        "pipelines/100/jobs": _make_response(500, {}, "boom"),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    with pytest.raises(real_httpx.HTTPStatusError):
        forge.list_workflow_run_jobs(run_id=100)


# ---------------------------------------------------------------------------
# has_online_runner
# ---------------------------------------------------------------------------


def test_has_online_runner_true_when_runner_returned(tmp_path, monkeypatch):
    """GET /projects/:id/runners?status=online returns a runner → True."""
    project_json = {"id": 42}
    get_map = {
        "projects/42/runners": _make_response(200, [{"id": 5, "status": "online"}]),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    assert forge.has_online_runner() is True


def test_has_online_runner_false_when_empty(tmp_path, monkeypatch):
    """No online runners → False."""
    project_json = {"id": 42}
    get_map = {
        "projects/42/runners": _make_response(200, []),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    assert forge.has_online_runner() is False


def test_has_online_runner_error_returns_false(tmp_path, monkeypatch):
    """Any lookup error → False, never raises."""
    project_json = {"id": 42}
    get_map = {
        "projects/42/runners": _make_response(500, {}, "boom"),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    assert forge.has_online_runner() is False


# ---------------------------------------------------------------------------
# dispatch_workflow — create a pipeline on a ref
# ---------------------------------------------------------------------------


def test_dispatch_workflow_201_returns_dispatched_true(tmp_path, monkeypatch):
    """POST /projects/:id/pipeline → 201 → {"dispatched": True}."""
    project_json = {"id": 42}
    get_map = {"projects/ns%2Fproject": _make_response(200, project_json)}
    captured = _mock_httpx(
        monkeypatch,
        get_map=get_map,
        post_response=_make_response(201, {"id": 100}),
    )

    forge = _forge(tmp_path)
    result = forge.dispatch_workflow(workflow_file="ci.yml", ref="main")
    assert result == {"dispatched": True}
    assert "/projects/42/pipeline" in captured["post_url"]
    assert captured["post_payload"] == {"ref": "main"}


def test_dispatch_workflow_non_success_returns_dispatched_false(tmp_path, monkeypatch):
    """A non-2xx response → {"dispatched": False, "reason": ...}."""
    project_json = {"id": 42}
    get_map = {"projects/ns%2Fproject": _make_response(200, project_json)}
    _mock_httpx(
        monkeypatch,
        get_map=get_map,
        post_response=_make_response(400, {"message": "bad ref"}),
    )

    forge = _forge(tmp_path)
    result = forge.dispatch_workflow(workflow_id=7, ref="main")
    assert result["dispatched"] is False
    assert "HTTP 400" in result["reason"]


def test_dispatch_workflow_resolve_failure_returns_dispatched_false(
    tmp_path, monkeypatch
):
    """Project-resolution failure is caught → {"dispatched": False}."""
    _mock_httpx(monkeypatch, get_map={})

    forge = _forge(tmp_path)
    result = forge.dispatch_workflow(ref="main")
    assert result["dispatched"] is False
    assert result["reason"]


# ---------------------------------------------------------------------------
# get_workflow_triggers — schedule + .gitlab-ci.yml workflow:rules
# ---------------------------------------------------------------------------


def test_get_workflow_triggers_cron_only(tmp_path, monkeypatch):
    """Active schedule + workflow:rules restricted to schedule → cron-only
    trigger set (schedule + workflow_dispatch, no push/pull_request)."""
    project_json = {"id": 42}
    ci_yaml = (
        "workflow:\n"
        "  rules:\n"
        "    - if: '$CI_PIPELINE_SOURCE == \"schedule\"'\n"
        "    - if: '$CI_PIPELINE_SOURCE == \"web\"'\n"
    )
    get_map = {
        "pipeline_schedules": _make_response(200, [{"id": 1, "active": True}]),
        ".gitlab-ci.yml": _make_response(200, {}, ci_yaml),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    from robotsix_mill.agents.runners.ci_auto_close_runner import _is_cron_only

    forge = _forge(tmp_path)
    triggers = forge.get_workflow_triggers(path="ignored")
    assert triggers is not None
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert _is_cron_only(triggers) is True


def test_get_workflow_triggers_push_disqualifies_cron_only(tmp_path, monkeypatch):
    """A push rule → push trigger present → not cron-only."""
    project_json = {"id": 42}
    ci_yaml = (
        "workflow:\n"
        "  rules:\n"
        "    - if: '$CI_PIPELINE_SOURCE == \"schedule\"'\n"
        "    - if: '$CI_PIPELINE_SOURCE == \"push\"'\n"
    )
    get_map = {
        "pipeline_schedules": _make_response(200, [{"id": 1}]),
        ".gitlab-ci.yml": _make_response(200, {}, ci_yaml),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    from robotsix_mill.agents.runners.ci_auto_close_runner import _is_cron_only

    forge = _forge(tmp_path)
    triggers = forge.get_workflow_triggers(path="ignored")
    assert "push" in (triggers or [])
    assert _is_cron_only(triggers) is False


def test_get_workflow_triggers_no_rules_defaults_to_push(tmp_path, monkeypatch):
    """No workflow:rules and no schedule → push (GitLab's default)."""
    project_json = {"id": 42}
    get_map = {
        "pipeline_schedules": _make_response(200, []),
        ".gitlab-ci.yml": _make_response(200, {}, "stages:\n  - build\n"),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    triggers = forge.get_workflow_triggers(path="ignored")
    assert "push" in (triggers or [])


def test_get_workflow_triggers_error_returns_none(tmp_path, monkeypatch):
    """Project-resolution failure → None, never raises."""
    _mock_httpx(monkeypatch, get_map={})

    forge = _forge(tmp_path)
    assert forge.get_workflow_triggers(path="ignored") is None


# ---------------------------------------------------------------------------
# get_workflow_file / list_path_commits_since — stale-CI gate parity
# ---------------------------------------------------------------------------


def test_get_workflow_file_returns_raw_text(tmp_path, monkeypatch):
    get_map = {
        "repository/files/.gitlab-ci.yml/raw": _make_response(
            200, {}, "stages:\n  - build\n"
        ),
        "projects/ns%2Fproject": _make_response(200, {"id": 42}),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    assert forge.get_workflow_file(path=".gitlab-ci.yml") == "stages:\n  - build\n"


def test_get_workflow_file_error_returns_none(tmp_path, monkeypatch):
    """Missing file (404) → None, never raises."""
    get_map = {"projects/ns%2Fproject": _make_response(200, {"id": 42})}
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    assert forge.get_workflow_file(path="ci/missing.yml") is None


def test_list_path_commits_since_returns_ids(tmp_path, monkeypatch):
    commits = [{"id": "abc123"}, {"id": "def456"}, {"title": "no id"}]
    get_map = {
        "repository/commits": _make_response(200, commits),
        "projects/ns%2Fproject": _make_response(200, {"id": 42}),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.list_path_commits_since(
        path=".gitlab-ci.yml", branch="main", since="2026-01-01T00:00:00+00:00"
    )
    assert result == ["abc123", "def456"]


def test_list_path_commits_since_no_commits_returns_empty(tmp_path, monkeypatch):
    """No commits touched the path since *since* → [] (not None)."""
    get_map = {
        "repository/commits": _make_response(200, []),
        "projects/ns%2Fproject": _make_response(200, {"id": 42}),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.list_path_commits_since(
        path=".gitlab-ci.yml", branch="main", since="2026-01-01T00:00:00+00:00"
    )
    assert result == []


def test_list_path_commits_since_error_returns_none(tmp_path, monkeypatch):
    _mock_httpx(monkeypatch, get_map={})

    forge = _forge(tmp_path)
    assert (
        forge.list_path_commits_since(
            path=".gitlab-ci.yml", branch="main", since="2026-01-01T00:00:00+00:00"
        )
        is None
    )


def test_gitlab_ci_triggers_helper_schedule_from_active_flag():
    """The helper adds 'schedule' when an active schedule exists even with
    no workflow:rules block."""
    from robotsix_mill.scm.forge.gitlab.ci import _gitlab_ci_triggers

    triggers = _gitlab_ci_triggers("", has_active_schedule=True)
    assert triggers is not None
    assert "schedule" in triggers
    assert "workflow_dispatch" in triggers


# ---------------------------------------------------------------------------
# read_repo_file / list_repo_files (fleet-file tools parity with GitHub)
# ---------------------------------------------------------------------------


class _RecordingHttp:
    """Fake ``_ApiClient`` for the repo-file seams: records GET calls and
    serves the project lookup, raw file, and paginated tree endpoints."""

    def __init__(self, *, tree_pages=None, fail=False):
        self.calls: list = []
        self.tree_pages = list(tree_pages or [])
        self.fail = fail

    def _resp(self, url):
        if self.fail:
            return _make_response(500, {}, "boom")
        if url.endswith("/raw"):
            return _make_response(200, None, text="hi")
        if "/repository/tree" in url:
            return _make_response(200, self.tree_pages.pop(0))
        return _make_response(200, {"id": 7, "default_branch": "trunk"})

    def get(self, path, **kw):
        self.calls.append((path, kw))
        return self._resp(path)

    def client(self):
        from contextlib import contextmanager

        outer = self

        class _C:
            def get(self, url, headers=None, params=None):
                outer.calls.append((url, {"params": params}))
                return outer._resp(url)

        @contextmanager
        def _cm():
            yield _C(), "https://gl/api/v4", {}

        return _cm()


def test_gitlab_read_repo_file_get_only_and_default_branch(tmp_path):
    forge = _forge(tmp_path)
    http = _RecordingHttp()
    forge._http = http
    assert forge.read_repo_file(path="x y/a.yml") == "hi"
    assert forge.read_repo_file(path="a.yml", ref="v1") == "hi"
    assert http.calls[0] == ("/projects/ns%2Fproject", {})
    assert http.calls[1] == (
        "/projects/ns%2Fproject/repository/files/x%20y%2Fa.yml/raw",
        {"params": {"ref": "trunk"}},
    )
    assert http.calls[2] == (
        "/projects/ns%2Fproject/repository/files/a.yml/raw",
        {"params": {"ref": "v1"}},
    )
    assert len(http.calls) == 3


def test_gitlab_list_repo_files_paginates_and_keeps_blobs(tmp_path):
    forge = _forge(tmp_path)
    page1 = [{"type": "blob", "path": f"f{i}"} for i in range(99)] + [
        {"type": "tree", "path": "dir"}
    ]
    page2 = [{"type": "blob", "path": "dir/last.yml"}]
    http = _RecordingHttp(tree_pages=[page1, page2])
    forge._http = http
    files = forge.list_repo_files(ref="v1")
    assert files == [f"f{i}" for i in range(99)] + ["dir/last.yml"]
    assert len(http.calls) == 2
    url, kw = http.calls[0]
    assert url == "https://gl/api/v4/projects/ns%2Fproject/repository/tree"
    assert kw["params"]["recursive"] == "true"
    assert kw["params"]["ref"] == "v1"


def test_gitlab_list_repo_files_omits_empty_ref(tmp_path):
    forge = _forge(tmp_path)
    http = _RecordingHttp(tree_pages=[[{"type": "blob", "path": "a"}]])
    forge._http = http
    assert forge.list_repo_files() == ["a"]
    assert "ref" not in http.calls[0][1]["params"]


def test_gitlab_read_repo_file_missing_file_returns_none(tmp_path):
    """A 404 on the raw-file endpoint (missing file) → None."""

    class _MissingHttp(_RecordingHttp):
        def _resp(self, url):
            if url.endswith("/raw"):
                return _make_response(404, {"message": "404 File Not Found"})
            return super()._resp(url)

    forge = _forge(tmp_path)
    forge._http = _MissingHttp()
    assert forge.read_repo_file(path="nope.yml") is None
    assert forge.read_repo_file(path="nope.yml", ref="main") is None


def test_gitlab_repo_file_reads_never_raise(tmp_path):
    forge = _forge(tmp_path)
    forge._http = _RecordingHttp(fail=True)
    assert forge.read_repo_file(path="a.yml") is None
    assert forge.read_repo_file(path="a.yml", ref="main") is None
    assert forge.list_repo_files() is None
