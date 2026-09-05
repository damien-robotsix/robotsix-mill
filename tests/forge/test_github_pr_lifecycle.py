"""Test GitHubForge PR-lifecycle HTTP seams with mocked httpx.Client.

Covers the primary merge-request seam — _create_pr, _get_pr,
pr_status_by_url, and _check_status — split out from test_github.py. No
stage-level monkeypatching: tests call the forge methods directly with a
mocked transport.
"""

import httpx as real_httpx
import pytest

from robotsix_mill.config import Secrets, Settings, _reset_secrets
from robotsix_mill.forge.github import GitHubForge

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
    kw.setdefault("forge_kind", "github")
    kw.setdefault("forge_remote_url", "https://github.com/o/r.git")
    kw.setdefault("FORGE_TOKEN", "tok")
    # Mirror forge_token into Secrets so get_secrets() works
    ft = kw.get("FORGE_TOKEN")
    if ft is not None:
        _set_secrets(forge_token=ft)
    # FORGE_TOKEN is now a Secrets-only field; pop before Settings()
    kw.pop("FORGE_TOKEN", None)
    s = Settings(**kw)
    return s


def _forge(tmp_path, **kw):
    return GitHubForge(_settings(tmp_path, **kw))


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


def _mock_httpx(monkeypatch, *, post_response=None, get_map=None, patch_response=None):
    """Replace httpx.Client with a controllable mock.

    *post_response*: returned for every POST call.
    *get_map*: dict mapping URL substrings → FakeResponse for GET calls.
    *patch_response*: returned for every PATCH call.
    """
    captured = {
        "post_payload": None,
        "post_url": None,
        "patch_payload": None,
        "patch_url": None,
    }

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

        def patch(self, url, headers=None, json=None, **kwargs):
            captured["patch_payload"] = json
            captured["patch_url"] = url
            return patch_response or _make_response(500, {}, "error")

        def get(self, url, headers=None, params=None, **kwargs):
            if get_map:
                for key, resp in get_map.items():
                    if key in url:
                        return resp
            return _make_response(404, [], "")

    monkeypatch.setattr(real_httpx, "Client", MockClient)
    return captured


# ---------------------------------------------------------------------------
# _create_pr (via open_merge_request)
# ---------------------------------------------------------------------------


def test_create_pr_201_returns_html_url(tmp_path, monkeypatch):
    fake_json = {"html_url": "https://github.com/o/r/pull/42"}
    _mock_httpx(monkeypatch, post_response=_make_response(201, fake_json))

    forge = _forge(tmp_path)
    url = forge.open_merge_request(source_branch="feature/x", title="t", body="b")
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
    url = forge.open_merge_request(source_branch="feature/x", title="t", body="b")
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
        forge.open_merge_request(source_branch="feature/x", title="t", body="b")


def test_create_pr_422_throttled_lookup_raises_transient_not_422(tmp_path, monkeypatch):
    """422 "already exists" + throttled lookup → surface the throttle.

    GitHub refused the *read* that would have recovered the existing PR's
    URL, so concluding "no such PR" and raising the create's 422 is a lie:
    it blocks the ticket for a human when the PR is sitting right there.
    Re-raising the lookup failure lets the stage classifier see a transient
    error and retry with backoff instead.
    """
    from robotsix_mill.runtime.transient_errors import classify_stage_error

    post_422 = _make_response(422, {}, "A pull request already exists for o:feature/x")
    throttled = _make_response(
        403, {}, '{"message":"You have exceeded a secondary rate limit"}'
    )
    _mock_httpx(
        monkeypatch, post_response=post_422, get_map={"repos/o/r/pulls": throttled}
    )

    forge = _forge(tmp_path)
    with pytest.raises(real_httpx.HTTPStatusError) as excinfo:
        forge.open_merge_request(source_branch="feature/x", title="t", body="b")
    assert classify_stage_error(excinfo.value) == "transient"


def test_create_pr_422_finds_closed_pr_for_head(tmp_path, monkeypatch):
    """A closed PR for the head also makes GitHub reject the create.

    The lookup therefore queries ``state=all``; an ``open``-only filter
    cannot see it and the ticket blocked on a PR that plainly exists.
    """
    post_422 = _make_response(422, {}, "already exists")
    closed_pr = [
        {"html_url": "https://github.com/o/r/pull/7", "number": 7, "state": "closed"}
    ]
    _mock_httpx(
        monkeypatch,
        post_response=post_422,
        get_map={"repos/o/r/pulls": _make_response(200, closed_pr)},
    )

    forge = _forge(tmp_path)
    url = forge.open_merge_request(source_branch="feature/x", title="t", body="b")
    assert url == "https://github.com/o/r/pull/7"


def test_create_pr_non_201_non_422_raises(tmp_path, monkeypatch):
    """Any other status → RuntimeError."""
    _mock_httpx(monkeypatch, post_response=_make_response(403, {}, "forbidden"))

    forge = _forge(tmp_path)
    with pytest.raises(RuntimeError, match="GitHub PR create failed"):
        forge.open_merge_request(source_branch="feature/x", title="t", body="b")


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
    assert payload["base"] == "main"  # default forge_target_branch
    assert payload["title"] == "My Title"
    assert payload["body"] == "My Body"
    # All expected keys present and no extras
    assert set(payload.keys()) == {"head", "base", "title", "body"}


def test_create_pr_base_honors_repo_working_branch(tmp_path, monkeypatch):
    """A repo_config.working_branch must become the PR base — regression
    for ros2-example-interfaces 5a2a, where the deliver stage computed the
    target as ``lyrical`` but the forge re-derived ``main`` from settings
    and GitHub answered 422 base-invalid."""
    from robotsix_mill.config import RepoConfig

    captured = _mock_httpx(
        monkeypatch,
        post_response=_make_response(201, {"html_url": "http://x"}),
    )
    rc = RepoConfig(
        repo_id="r",
        board_id="b",
        langfuse_project_name="r",
        langfuse_public_key="",
        langfuse_secret_key="",
        working_branch="lyrical",
    )
    forge = GitHubForge(_settings(tmp_path), repo_config=rc)
    forge.open_merge_request(source_branch="feature/x", title="t", body="b")

    assert captured["post_payload"]["base"] == "lyrical"


def test_create_pr_cross_fork_head_and_upstream_base(tmp_path, monkeypatch):
    """A cross_repo_target opens the PR fork→upstream: POST targets the
    upstream owner/repo, head is ``<fork-owner>:<branch>``, base is the
    target's base_branch."""
    from robotsix_mill.config import CrossRepoTarget, RepoConfig

    captured = _mock_httpx(
        monkeypatch,
        post_response=_make_response(
            201, {"html_url": "https://github.com/up/r/pull/7"}
        ),
    )
    rc = RepoConfig(
        repo_id="r",
        board_id="b",
        langfuse_project_name="r",
        langfuse_public_key="",
        langfuse_secret_key="",
        cross_repo_target=CrossRepoTarget(
            upstream_remote_url="https://github.com/up/r.git",
            fork_remote_url="https://github.com/fork/r.git",
            base_branch="develop",
        ),
    )
    forge = GitHubForge(_settings(tmp_path), repo_config=rc)
    url = forge.open_merge_request(
        source_branch="feature/x", title="t", body="b", head_repo="fork/r"
    )

    assert url == "https://github.com/up/r/pull/7"
    assert "repos/up/r/pulls" in captured["post_url"]
    payload = captured["post_payload"]
    assert payload["head"] == "fork:feature/x"
    assert payload["base"] == "develop"


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
        "mergeable_state": "clean",
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
        "mergeable_state": "clean",
        "sha": "abc123",
        "number": 7,
        "author": "",
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
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, headers=None, json=None):
            return _make_response(500, {}, "")

        def get(self, url, headers=None, params=None):
            calls.append(url)
            if "/pulls/7" in url:
                return _make_response(
                    200,
                    {
                        "number": 7,
                        "merged": True,
                        "state": "closed",
                        "html_url": "http://pr/7",
                        "mergeable": None,
                        "head": {"sha": "def456"},
                    },
                )
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
# pr_status_by_url (URL-keyed fallback via _get_pr_by_number)
# ---------------------------------------------------------------------------


def test_pr_status_by_url_resolves_merged_pr(tmp_path, monkeypatch):
    """A recorded PR url resolves by number to its current status,
    independent of whether the head branch still exists."""
    detail_resp = {
        "number": 7,
        "merged": True,
        "state": "closed",
        "html_url": "http://gh/o/r/pull/7",
        "mergeable": None,
        "mergeable_state": "unknown",
        "head": {"sha": "abc123"},
    }
    get_map = {"repos/o/r/pulls/7": _make_response(200, detail_resp)}
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    status = forge.pr_status_by_url(url="http://gh/o/r/pull/7")
    assert status == {
        "merged": True,
        "state": "closed",
        "url": "http://gh/o/r/pull/7",
        "mergeable": None,
        "mergeable_state": "unknown",
        "sha": "abc123",
        "number": 7,
        "author": "",
    }


def test_pr_status_by_url_unparseable_returns_none(tmp_path, monkeypatch):
    """A url that does not contain ``/pull/<n>`` → None (no API call)."""
    _mock_httpx(monkeypatch, get_map={})

    forge = _forge(tmp_path)
    assert forge.pr_status_by_url(url="https://github.com/o/r") is None


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
        # check_status now always probes combined statuses to
        # distinguish "no CI configured" from "CI pending".
        "commits/abc123/status": _make_response(200, {"statuses": []}),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.check_status(source_branch="feature/x")
    assert result is not None
    assert "conclusion" in result
    assert "failing" in result
    assert "jobs" in result
    assert result["conclusion"] == "success"
    assert result["failing"] == []
    assert result["jobs"] == [{"name": "CI / test", "conclusion": "success"}]


def test_check_status_parse_failed_workflow_fails_gate(tmp_path, monkeypatch):
    """A workflow that fails at PARSE registers no check-run, so check-runs
    read green — but its failing Actions-API run must still fail the gate.

    Regression for cost-monitor 27a2: ci.yml/release.yml failed at parse
    (invalid ``uses:``), so zero of their jobs ran; only lint-workflows +
    CodeQL registered check-runs (both green) and the gate flagged CI-green
    + mergeable.
    """
    list_resp = [{"number": 3}]
    detail_resp = {
        "number": 3,
        "merged": False,
        "state": "open",
        "html_url": "http://pr/3",
        "mergeable": True,
        "head": {"sha": "abc123"},
    }
    # Only the workflows that PARSED registered check-runs — both green.
    check_runs_resp = {
        "check_runs": [
            {
                "id": 101,
                "name": "lint-workflows",
                "status": "completed",
                "conclusion": "success",
                "output": {"summary": None, "text": None, "annotations": []},
            },
            {
                "id": 102,
                "name": "CodeQL",
                "status": "completed",
                "conclusion": "success",
                "output": {"summary": None, "text": None, "annotations": []},
            },
        ]
    }
    # The Actions API DOES show the parse-failed run — its name is the
    # workflow file path (GitHub cannot read the ``name:`` field it failed
    # to parse).
    runs_resp = {
        "workflow_runs": [
            {
                "id": 9,
                "name": ".github/workflows/ci.yml",
                "workflow_id": 55,
                "head_sha": "abc123",
                "conclusion": "failure",
                "html_url": "http://run/9",
                "created_at": "2025-01-01T00:00:00Z",
                "event": "pull_request",
                "head_branch": "feature/x",
            }
        ]
    }
    get_map = {
        "repos/o/r/pulls/3": _make_response(200, detail_resp),
        "repos/o/r/pulls": _make_response(200, list_resp),
        "commits/abc123/check-runs": _make_response(200, check_runs_resp),
        "commits/abc123/status": _make_response(200, {"statuses": []}),
        "actions/runs": _make_response(200, runs_resp),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.check_status(source_branch="feature/x")
    assert result is not None
    assert result["conclusion"] == "failure"
    names = [f["name"] for f in result["failing"]]
    assert ".github/workflows/ci.yml" in names


def test_check_status_green_workflow_run_stays_green(tmp_path, monkeypatch):
    """A workflow RUN that concluded success must not flip a green gate."""
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
                "output": {"summary": None, "text": None, "annotations": []},
            }
        ]
    }
    runs_resp = {
        "workflow_runs": [
            {
                "id": 9,
                "name": "CI",
                "workflow_id": 55,
                "head_sha": "abc123",
                "conclusion": "success",
                "html_url": "http://run/9",
                "created_at": "2025-01-01T00:00:00Z",
                "event": "pull_request",
                "head_branch": "feature/x",
            }
        ]
    }
    get_map = {
        "repos/o/r/pulls/3": _make_response(200, detail_resp),
        "repos/o/r/pulls": _make_response(200, list_resp),
        "commits/abc123/check-runs": _make_response(200, check_runs_resp),
        "commits/abc123/status": _make_response(200, {"statuses": []}),
        "actions/runs": _make_response(200, runs_resp),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.check_status(source_branch="feature/x")
    assert result is not None
    assert result["conclusion"] == "success"
    assert result["failing"] == []
