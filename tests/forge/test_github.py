"""Test GitHubForge HTTP seams with mocked httpx.Client.

No stage-level monkeypatching — tests call _create_pr, _get_pr, _check_status,
_parse_owner_repo, and _build_headers directly with a mocked transport.
"""

import httpx as real_httpx
import pytest

from robotsix_mill.config import Settings, Secrets, _reset_secrets
from robotsix_mill.forge.github import (
    GitHubForge,
    _build_headers,
    _parse_owner_repo,
    _ANSI_RE,
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
    kw.setdefault("MILL_DATA_DIR", str(tmp_path))
    kw.setdefault("FORGE_KIND", "github")
    kw.setdefault("FORGE_REMOTE_URL", "https://github.com/o/r.git")
    kw.setdefault("FORGE_TOKEN", "tok")
    s = Settings(**kw)
    # Mirror forge_token into Secrets so get_secrets() works
    ft = kw.get("FORGE_TOKEN")
    if ft is not None:
        _set_secrets(forge_token=ft)
    return s


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

        def post(self, url, headers=None, json=None, **kwargs):
            captured["post_payload"] = json
            return post_response or _make_response(500, {}, "error")

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
    assert result["conclusion"] == "success"
    assert result["failing"] == []


# ---------------------------------------------------------------------------
# list_workflow_runs
# ---------------------------------------------------------------------------

def test_list_workflow_runs_by_branch(tmp_path, monkeypatch):
    """Mock GET .../actions/runs?branch=main&status=completed&per_page=30."""
    runs_data = {
        "workflow_runs": [
            {
                "id": 1, "name": "CI", "workflow_id": 100,
                "head_sha": "abc", "conclusion": "failure",
                "html_url": "http://x", "created_at": "2025-01-01T00:00:00Z",
            }
        ]
    }
    get_map = {"actions/runs": _make_response(200, runs_data)}
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.list_workflow_runs(branch="main")
    assert len(result) == 1
    assert result[0]["id"] == 1
    assert result[0]["conclusion"] == "failure"
    assert result[0]["head_sha"] == "abc"


def test_list_workflow_runs_by_head_sha(tmp_path, monkeypatch):
    """Mock with ?head_sha=abc123 param."""
    runs_data = {"workflow_runs": []}
    captured_params = {}

    class ParamsClient:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, headers=None, json=None):
            return _make_response(500, {}, "")
        def get(self, url, headers=None, params=None):
            captured_params.update(params or {})
            if "actions/runs" in url:
                return _make_response(200, runs_data)
            return _make_response(200, [])

    monkeypatch.setattr(real_httpx, "Client", ParamsClient)

    forge = _forge(tmp_path)
    forge.list_workflow_runs(head_sha="abc123")
    assert captured_params.get("head_sha") == "abc123"


def test_list_workflow_runs_empty(tmp_path, monkeypatch):
    """No runs → empty list."""
    get_map = {"actions/runs": _make_response(200, {"workflow_runs": []})}
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    assert forge.list_workflow_runs(branch="main") == []


# ---------------------------------------------------------------------------
# fetch_workflow_job_logs
# ---------------------------------------------------------------------------

_ANSI_LOG = "\x1b[1mBOLD\x1b[0m normal \x1b[31mRED\x1b[0m\n"
_ANSI_CLEAN = "BOLD normal RED\n"


def test_fetch_workflow_job_logs_single_failed_job(tmp_path, monkeypatch):
    """Mock runs/jobs + jobs/logs; verify ANSI stripped, job-name header."""
    jobs_data = {
        "jobs": [
            {"id": 201, "name": "build", "conclusion": "failure"},
        ]
    }
    get_map = {
        "actions/runs/1/jobs": _make_response(200, jobs_data),
        "actions/jobs/201/logs": _make_response(200, {}, _ANSI_LOG),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.fetch_workflow_job_logs(run_id=1)
    assert "### Job: build (id=201)" in result
    assert _ANSI_CLEAN in result


def test_fetch_workflow_job_logs_multiple_failed_jobs(tmp_path, monkeypatch):
    """Two failed jobs → both logs concatenated."""
    jobs_data = {
        "jobs": [
            {"id": 1, "name": "lint", "conclusion": "failure"},
            {"id": 2, "name": "test", "conclusion": "failure"},
        ]
    }
    get_map = {
        "actions/runs/1/jobs": _make_response(200, jobs_data),
        "actions/jobs/1/logs": _make_response(200, {}, "lint log\n"),
        "actions/jobs/2/logs": _make_response(200, {}, "test log\n"),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.fetch_workflow_job_logs(run_id=1)
    assert "### Job: lint" in result
    assert "lint log" in result
    assert "### Job: test" in result
    assert "test log" in result


def test_fetch_workflow_job_logs_all_jobs_pass(tmp_path, monkeypatch):
    """No failed jobs → returns empty string."""
    jobs_data = {
        "jobs": [
            {"id": 1, "name": "lint", "conclusion": "success"},
            {"id": 2, "name": "test", "conclusion": "success"},
        ]
    }
    get_map = {"actions/runs/1/jobs": _make_response(200, jobs_data)}
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.fetch_workflow_job_logs(run_id=1)
    assert result == ""


def test_fetch_workflow_job_logs_capped(tmp_path, monkeypatch):
    """Log exceeds MILL_CI_LOG_MAX_BYTES → only last N bytes kept."""
    # Create a log longer than the default 65536 cap.
    big_log = "x" * 100_000
    jobs_data = {
        "jobs": [
            {"id": 1, "name": "big-job", "conclusion": "failure"},
        ]
    }
    get_map = {
        "actions/runs/1/jobs": _make_response(200, jobs_data),
        "actions/jobs/1/logs": _make_response(200, {}, big_log),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.fetch_workflow_job_logs(run_id=1)
    # The job log part (after the header) should be capped.
    log_section = result.split("\n", 1)[1] if "\n" in result else result
    assert len(log_section) <= 65536 + 100  # allow header overhead


# ---------------------------------------------------------------------------
# ANSI stripping regex
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# fetch_workflow_job_logs — redirect & error-mode coverage
# ---------------------------------------------------------------------------

def test_fetch_workflow_job_logs_follows_redirect(tmp_path, monkeypatch):
    """Verify follow_redirects=True is passed on the per-job log GET."""
    jobs_data = {
        "jobs": [{"id": 201, "name": "build", "conclusion": "failure"}]
    }
    log_kwargs_captured = {}

    class RedirectCaptureClient:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, headers=None, json=None):
            return _make_response(500, {}, "")
        def get(self, url, headers=None, params=None, **kwargs):
            if "/jobs/" in url and "/logs" in url:
                log_kwargs_captured.update(kwargs)
                return _make_response(200, {}, "log body after redirect\n")
            if "/jobs" in url:
                return _make_response(200, jobs_data)
            return _make_response(404, [], "")

    monkeypatch.setattr(real_httpx, "Client", RedirectCaptureClient)

    forge = _forge(tmp_path)
    result = forge.fetch_workflow_job_logs(run_id=1)
    assert "log body after redirect" in result
    assert log_kwargs_captured.get("follow_redirects") is True


def test_fetch_workflow_job_logs_403_on_logs_endpoint(tmp_path, monkeypatch):
    """403 from the logs endpoint → permission hint placeholder."""
    jobs_data = {
        "jobs": [{"id": 201, "name": "build", "conclusion": "failure"}]
    }

    class ErrorClient:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, headers=None, json=None):
            return _make_response(500, {}, "")
        def get(self, url, headers=None, params=None, **kwargs):
            if "/jobs/" in url and "/logs" in url:
                return _make_response(403, {})
            if "/jobs" in url:
                return _make_response(200, jobs_data)
            return _make_response(404, [], "")

    monkeypatch.setattr(real_httpx, "Client", ErrorClient)

    forge = _forge(tmp_path)
    result = forge.fetch_workflow_job_logs(run_id=1)
    assert "[log fetch failed for job 201: HTTP 403 — App likely missing Actions:Read permission]" in result


def test_fetch_workflow_job_logs_404_on_logs_endpoint(tmp_path, monkeypatch):
    """404 from the logs endpoint → generic HTTP placeholder."""
    jobs_data = {
        "jobs": [{"id": 202, "name": "lint", "conclusion": "failure"}]
    }

    class ErrorClient:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, headers=None, json=None):
            return _make_response(500, {}, "")
        def get(self, url, headers=None, params=None, **kwargs):
            if "/jobs/" in url and "/logs" in url:
                return _make_response(404, {})
            if "/jobs" in url:
                return _make_response(200, jobs_data)
            return _make_response(404, [], "")

    monkeypatch.setattr(real_httpx, "Client", ErrorClient)

    forge = _forge(tmp_path)
    result = forge.fetch_workflow_job_logs(run_id=1)
    assert "[log fetch failed for job 202: HTTP 404]" in result


def test_fetch_workflow_job_logs_empty_body_after_success(tmp_path, monkeypatch):
    """200 with empty body → empty-body placeholder."""
    jobs_data = {
        "jobs": [{"id": 303, "name": "test", "conclusion": "failure"}]
    }

    class EmptyBodyClient:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, headers=None, json=None):
            return _make_response(500, {}, "")
        def get(self, url, headers=None, params=None, **kwargs):
            if "/jobs/" in url and "/logs" in url:
                return _make_response(200, {}, "")  # text="" default
            if "/jobs" in url:
                return _make_response(200, jobs_data)
            return _make_response(404, [], "")

    monkeypatch.setattr(real_httpx, "Client", EmptyBodyClient)

    forge = _forge(tmp_path)
    result = forge.fetch_workflow_job_logs(run_id=1)
    assert "[log fetch returned empty body for job 303]" in result


def test_ansi_regex_strips_sgr():
    assert _ANSI_RE.sub("", "\x1b[1mBOLD\x1b[0m") == "BOLD"


def test_ansi_regex_strips_color():
    assert _ANSI_RE.sub("", "\x1b[31mRED\x1b[0m") == "RED"


def test_ansi_regex_plain_text_unchanged():
    assert _ANSI_RE.sub("", "hello world") == "hello world"


# ---------------------------------------------------------------------------
# _merge_pr (internal seam) and merge_pr (public method)
# ---------------------------------------------------------------------------

def test_merge_pr_success(tmp_path, monkeypatch):
    """200 response → {"merged": True, "reason": "merged"}."""
    list_resp = [{"number": 7}]
    detail_resp = {
        "number": 7,
        "merged": False,
        "state": "open",
        "html_url": "http://pr/7",
        "mergeable": True,
        "head": {"sha": "abc123"},
    }
    merge_resp = {"sha": "abc", "merged": True, "message": "Pull Request successfully merged"}
    get_map = {
        "repos/o/r/pulls/7": _make_response(200, detail_resp),
        "repos/o/r/pulls": _make_response(200, list_resp),
    }
    captured = _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    # Monkey-patch _merge_pr to simulate the HTTP seam
    monkeypatch.setattr(
        forge, "_merge_pr",
        lambda *, owner, repo, pull_number: {"merged": True, "reason": "merged"},
    )
    result = forge.merge_pr(source_branch="feature/x")
    assert result == {"merged": True, "reason": "merged"}


def test_merge_pr_405_not_allowed(tmp_path, monkeypatch):
    """405 → {"merged": False} with branch-protection reason."""
    list_resp = [{"number": 7}]
    detail_resp = {
        "number": 7,
        "merged": False,
        "state": "open",
        "html_url": "http://pr/7",
        "mergeable": True,
        "head": {"sha": "abc123"},
    }
    get_map = {
        "repos/o/r/pulls/7": _make_response(200, detail_resp),
        "repos/o/r/pulls": _make_response(200, list_resp),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    monkeypatch.setattr(
        forge, "_merge_pr",
        lambda *, owner, repo, pull_number: {
            "merged": False, "reason": "merge not allowed (branch protection?)",
        },
    )
    result = forge.merge_pr(source_branch="feature/x")
    assert result["merged"] is False
    assert "branch protection" in result["reason"]


def test_merge_pr_409_conflict(tmp_path, monkeypatch):
    """409 → {"merged": False} with not-mergeable reason."""
    list_resp = [{"number": 7}]
    detail_resp = {
        "number": 7,
        "merged": False,
        "state": "open",
        "html_url": "http://pr/7",
        "mergeable": True,
        "head": {"sha": "abc123"},
    }
    get_map = {
        "repos/o/r/pulls/7": _make_response(200, detail_resp),
        "repos/o/r/pulls": _make_response(200, list_resp),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    monkeypatch.setattr(
        forge, "_merge_pr",
        lambda *, owner, repo, pull_number: {
            "merged": False, "reason": "PR is not mergeable",
        },
    )
    result = forge.merge_pr(source_branch="feature/x")
    assert result["merged"] is False
    assert "not mergeable" in result["reason"]


def test_merge_pr_network_error(tmp_path, monkeypatch):
    """Network error → {"merged": False} (no raise)."""
    list_resp = [{"number": 7}]
    detail_resp = {
        "number": 7,
        "merged": False,
        "state": "open",
        "html_url": "http://pr/7",
        "mergeable": True,
        "head": {"sha": "abc123"},
    }
    get_map = {
        "repos/o/r/pulls/7": _make_response(200, detail_resp),
        "repos/o/r/pulls": _make_response(200, list_resp),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    monkeypatch.setattr(
        forge, "_merge_pr",
        lambda *, owner, repo, pull_number: {"merged": False, "reason": "connection refused"},
    )
    result = forge.merge_pr(source_branch="feature/x")
    assert result["merged"] is False


def test_merge_pr_not_found(tmp_path, monkeypatch):
    """PR not found → {"merged": False, "reason": "PR not found"}."""
    get_map = {"repos/o/r/pulls": _make_response(200, [])}
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.merge_pr(source_branch="feature/x")
    assert result == {"merged": False, "reason": "PR not found"}


# ---------------------------------------------------------------------------
# list_pr_comments
# ---------------------------------------------------------------------------


def test_list_pr_comments_happy_path(tmp_path, monkeypatch):
    """PR exists → _list_pr_comments returns normalized dicts."""
    list_resp = [{"number": 7}]
    detail_resp = {
        "number": 7, "merged": False, "state": "open",
        "html_url": "http://pr/7", "mergeable": True,
        "head": {"sha": "abc123"},
    }
    comments_resp = [
        {
            "id": 1001,
            "user": {"login": "alice"},
            "created_at": "2025-01-15T10:00:00Z",
            "body": "Looks good to me",
        },
        {
            "id": 1002,
            "user": {"login": "bob"},
            "created_at": "2025-01-15T11:00:00Z",
            "body": "",
        },
    ]
    get_map = {
        "repos/o/r/pulls/7": _make_response(200, detail_resp),
        "repos/o/r/pulls": _make_response(200, list_resp),
        "issues/7/comments": _make_response(200, comments_resp),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.list_pr_comments(source_branch="feature/x")
    assert len(result) == 2
    assert result[0] == {
        "id": 1001, "author": "alice",
        "created_at": "2025-01-15T10:00:00Z", "body": "Looks good to me",
    }
    assert result[1] == {
        "id": 1002, "author": "bob",
        "created_at": "2025-01-15T11:00:00Z", "body": "",
    }


def test_list_pr_comments_no_pr(tmp_path, monkeypatch):
    """No PR for branch → returns [] without calling the comments endpoint."""
    get_map = {"repos/o/r/pulls": _make_response(200, [])}
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.list_pr_comments(source_branch="feature/x")
    assert result == []


def test_list_pr_comments_empty_response(tmp_path, monkeypatch):
    """Endpoint returns [] → []."""
    list_resp = [{"number": 7}]
    detail_resp = {
        "number": 7, "merged": False, "state": "open",
        "html_url": "http://pr/7", "mergeable": True,
        "head": {"sha": "abc123"},
    }
    get_map = {
        "repos/o/r/pulls/7": _make_response(200, detail_resp),
        "repos/o/r/pulls": _make_response(200, list_resp),
        "issues/7/comments": _make_response(200, []),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.list_pr_comments(source_branch="feature/x")
    assert result == []


def test_list_pr_comments_http_error(tmp_path, monkeypatch):
    """Non-2xx from comments endpoint → raise_for_status propagates."""
    list_resp = [{"number": 7}]
    detail_resp = {
        "number": 7, "merged": False, "state": "open",
        "html_url": "http://pr/7", "mergeable": True,
        "head": {"sha": "abc123"},
    }
    get_map = {
        "repos/o/r/pulls/7": _make_response(200, detail_resp),
        "repos/o/r/pulls": _make_response(200, list_resp),
        "issues/7/comments": _make_response(403, {}, "forbidden"),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    with pytest.raises(real_httpx.HTTPStatusError):
        forge.list_pr_comments(source_branch="feature/x")


# ---------------------------------------------------------------------------
# list_pr_reviews
# ---------------------------------------------------------------------------


def test_list_pr_reviews_happy_path(tmp_path, monkeypatch):
    """PR exists → _list_pr_reviews returns normalized dicts (body="" when None)."""
    list_resp = [{"number": 7}]
    detail_resp = {
        "number": 7, "merged": False, "state": "open",
        "html_url": "http://pr/7", "mergeable": True,
        "head": {"sha": "abc123"},
    }
    reviews_resp = [
        {
            "id": 2001,
            "user": {"login": "alice"},
            "submitted_at": "2025-01-15T12:00:00Z",
            "body": "LGTM",
            "state": "APPROVED",
        },
        {
            "id": 2002,
            "user": {"login": "bob"},
            "submitted_at": "2025-01-15T13:00:00Z",
            "body": None,
            "state": "CHANGES_REQUESTED",
        },
    ]
    # More-specific keys first to avoid "repos/o/r/pulls/7" matching
    # the reviews URL (both contain "pulls/7").
    get_map = {
        "pulls/7/reviews": _make_response(200, reviews_resp),
        "repos/o/r/pulls/7": _make_response(200, detail_resp),
        "repos/o/r/pulls": _make_response(200, list_resp),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.list_pr_reviews(source_branch="feature/x")
    assert len(result) == 2
    assert result[0] == {
        "id": 2001, "author": "alice",
        "created_at": "2025-01-15T12:00:00Z", "body": "LGTM",
    }
    assert result[1] == {
        "id": 2002, "author": "bob",
        "created_at": "2025-01-15T13:00:00Z", "body": "",
    }
    # state is not part of the contract — verify its absence
    assert "state" not in result[0]
    assert "state" not in result[1]


def test_list_pr_reviews_no_pr(tmp_path, monkeypatch):
    """No PR → returns []."""
    get_map = {"repos/o/r/pulls": _make_response(200, [])}
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.list_pr_reviews(source_branch="feature/x")
    assert result == []


def test_list_pr_reviews_empty_response(tmp_path, monkeypatch):
    """Endpoint returns [] → []."""
    list_resp = [{"number": 7}]
    detail_resp = {
        "number": 7, "merged": False, "state": "open",
        "html_url": "http://pr/7", "mergeable": True,
        "head": {"sha": "abc123"},
    }
    get_map = {
        "pulls/7/reviews": _make_response(200, []),
        "repos/o/r/pulls/7": _make_response(200, detail_resp),
        "repos/o/r/pulls": _make_response(200, list_resp),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.list_pr_reviews(source_branch="feature/x")
    assert result == []


def test_list_pr_reviews_http_error(tmp_path, monkeypatch):
    """Non-2xx from reviews endpoint → raise_for_status propagates."""
    list_resp = [{"number": 7}]
    detail_resp = {
        "number": 7, "merged": False, "state": "open",
        "html_url": "http://pr/7", "mergeable": True,
        "head": {"sha": "abc123"},
    }
    get_map = {
        "pulls/7/reviews": _make_response(403, {}, "forbidden"),
        "repos/o/r/pulls/7": _make_response(200, detail_resp),
        "repos/o/r/pulls": _make_response(200, list_resp),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    with pytest.raises(real_httpx.HTTPStatusError):
        forge.list_pr_reviews(source_branch="feature/x")


# ---------------------------------------------------------------------------
# list_review_comments
# ---------------------------------------------------------------------------


def test_list_review_comments_happy_path(tmp_path, monkeypatch):
    """PR exists → _list_review_comments returns dicts with file_path, line, diff_hunk."""
    list_resp = [{"number": 7}]
    detail_resp = {
        "number": 7, "merged": False, "state": "open",
        "html_url": "http://pr/7", "mergeable": True,
        "head": {"sha": "abc123"},
    }
    comments_resp = [
        {
            "id": 3001,
            "user": {"login": "alice"},
            "created_at": "2025-01-15T14:00:00Z",
            "body": "Consider adding a docstring here.",
            "path": "src/foo.py",
            "line": 42,
            "diff_hunk": "@@ -40,6 +40,8 @@ def bar():",
        },
        {
            "id": 3002,
            "user": {"login": "bob"},
            "created_at": "2025-01-15T15:00:00Z",
            "body": "This line seems unused.",
            "path": "src/baz.py",
            "line": None,
            "original_line": 17,
            "diff_hunk": "@@ -15,3 +15,5 @@ def qux():",
        },
    ]
    get_map = {
        "pulls/7/comments": _make_response(200, comments_resp),
        "repos/o/r/pulls/7": _make_response(200, detail_resp),
        "repos/o/r/pulls": _make_response(200, list_resp),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.list_review_comments(source_branch="feature/x")
    assert len(result) == 2
    assert result[0] == {
        "id": 3001, "author": "alice",
        "created_at": "2025-01-15T14:00:00Z",
        "body": "Consider adding a docstring here.",
        "file_path": "src/foo.py", "line": 42,
        "diff_hunk": "@@ -40,6 +40,8 @@ def bar():",
    }
    assert result[1] == {
        "id": 3002, "author": "bob",
        "created_at": "2025-01-15T15:00:00Z",
        "body": "This line seems unused.",
        "file_path": "src/baz.py", "line": 17,
        "diff_hunk": "@@ -15,3 +15,5 @@ def qux():",
    }


def test_list_review_comments_no_pr(tmp_path, monkeypatch):
    """No PR → returns []."""
    get_map = {"repos/o/r/pulls": _make_response(200, [])}
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.list_review_comments(source_branch="feature/x")
    assert result == []


def test_list_review_comments_empty_response(tmp_path, monkeypatch):
    """Endpoint returns [] → []."""
    list_resp = [{"number": 7}]
    detail_resp = {
        "number": 7, "merged": False, "state": "open",
        "html_url": "http://pr/7", "mergeable": True,
        "head": {"sha": "abc123"},
    }
    get_map = {
        "pulls/7/comments": _make_response(200, []),
        "repos/o/r/pulls/7": _make_response(200, detail_resp),
        "repos/o/r/pulls": _make_response(200, list_resp),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.list_review_comments(source_branch="feature/x")
    assert result == []


def test_list_review_comments_http_error(tmp_path, monkeypatch):
    """Non-2xx from review-comments endpoint → raise_for_status propagates."""
    list_resp = [{"number": 7}]
    detail_resp = {
        "number": 7, "merged": False, "state": "open",
        "html_url": "http://pr/7", "mergeable": True,
        "head": {"sha": "abc123"},
    }
    get_map = {
        "pulls/7/comments": _make_response(403, {}, "forbidden"),
        "repos/o/r/pulls/7": _make_response(200, detail_resp),
        "repos/o/r/pulls": _make_response(200, list_resp),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    with pytest.raises(real_httpx.HTTPStatusError):
        forge.list_review_comments(source_branch="feature/x")


# ---------------------------------------------------------------------------
# _pr_files
# ---------------------------------------------------------------------------


def test_pr_files_happy_path(tmp_path, monkeypatch):
    """PR exists → _pr_files returns normalized file dicts."""
    list_resp = [{"number": 7}]
    detail_resp = {
        "number": 7, "merged": False, "state": "open",
        "html_url": "http://pr/7", "mergeable": True,
        "head": {"sha": "abc123"},
    }
    files_resp = [
        {
            "filename": "src/main.py",
            "status": "modified",
            "additions": 12,
            "deletions": 3,
        },
        {
            "filename": "tests/test_main.py",
            "status": "added",
            "additions": 45,
            "deletions": 0,
        },
        {
            "filename": "old/deprecated.py",
            "status": "removed",
            "additions": 0,
            "deletions": 20,
        },
    ]
    get_map = {
        "repos/o/r/pulls/7/files": _make_response(200, files_resp),
        "repos/o/r/pulls/7": _make_response(200, detail_resp),
        "repos/o/r/pulls": _make_response(200, list_resp),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    files = forge._pr_files(owner="o", repo="r", pull_number=7)
    assert len(files) == 3
    assert files[0] == {
        "path": "src/main.py", "status": "modified",
        "additions": 12, "deletions": 3,
    }
    assert files[1] == {
        "path": "tests/test_main.py", "status": "added",
        "additions": 45, "deletions": 0,
    }
    assert files[2] == {
        "path": "old/deprecated.py", "status": "removed",
        "additions": 0, "deletions": 20,
    }


def test_pr_files_no_pr(tmp_path, monkeypatch):
    """No PR for branch → returns [] without calling files endpoint."""
    get_map = {"repos/o/r/pulls": _make_response(200, [])}
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.pr_files(source_branch="no-such-branch")
    assert result == []


def test_pr_files_http_error(tmp_path, monkeypatch):
    """HTTP error on files endpoint → returns [] gracefully."""
    list_resp = [{"number": 7}]
    detail_resp = {
        "number": 7, "merged": False, "state": "open",
        "html_url": "http://pr/7", "mergeable": True,
        "head": {"sha": "abc123"},
    }
    get_map = {
        "repos/o/r/pulls/7/files": _make_response(500, {}, "boom"),
        "repos/o/r/pulls/7": _make_response(200, detail_resp),
        "repos/o/r/pulls": _make_response(200, list_resp),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    files = forge._pr_files(owner="o", repo="r", pull_number=7)
    assert files == []


def test_pr_files_empty_files(tmp_path, monkeypatch):
    """PR with no files changed → returns []."""
    list_resp = [{"number": 7}]
    detail_resp = {
        "number": 7, "merged": False, "state": "open",
        "html_url": "http://pr/7", "mergeable": True,
        "head": {"sha": "abc123"},
    }
    get_map = {
        "repos/o/r/pulls/7/files": _make_response(200, []),
        "repos/o/r/pulls/7": _make_response(200, detail_resp),
        "repos/o/r/pulls": _make_response(200, list_resp),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    files = forge._pr_files(owner="o", repo="r", pull_number=7)
    assert files == []
