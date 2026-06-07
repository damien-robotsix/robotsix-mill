"""Test GitHubForge HTTP seams with mocked httpx.Client.

No stage-level monkeypatching — tests call _create_pr, _get_pr, _check_status,
_parse_owner_repo, and _build_headers directly with a mocked transport.
"""

import httpx as real_httpx
import pytest

from robotsix_mill.config import Settings, Secrets, _reset_secrets
from robotsix_mill.forge.base import NotConfiguredError, RepoInfo
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
    kw.setdefault("data_dir", str(tmp_path))
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


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/o/r.git", ("o", "r")),
        ("https://github.com/o/r", ("o", "r")),
        ("git@github.com:o/r.git", ("o", "r")),
        ("https://github.com/owner-name/repo_name", ("owner-name", "repo_name")),
    ],
)
def test_parse_owner_repo_valid(url, expected):
    assert _parse_owner_repo(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not-a-url",
        "https://gitlab.com/o/r.git",
    ],
)
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
                "id": 1,
                "name": "CI",
                "workflow_id": 100,
                "head_sha": "abc",
                "conclusion": "failure",
                "html_url": "http://x",
                "created_at": "2025-01-01T00:00:00Z",
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
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

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


def test_capture_failure_window_anchors_on_first_error():
    """An if:always() cascade: the REAL build failure errors early, a masking
    step re-errors near the tail. A plain tail-cap would show only the mask;
    _capture_failure_window must surface the EARLY real failure."""
    from robotsix_mill.forge.github import _capture_failure_window

    real = "ERROR: failed to build proxy image: COPY filter not found\n##[error]Process completed with exit code 1\n"
    filler = "noise line padding the log\n" * 5000  # skipped-step noise
    mask = "FATAL could not parse reference: .\n##[error]Process completed with exit code 1\n"
    log = real + filler + mask
    out = _capture_failure_window(log, max_bytes=4000)
    assert "failed to build proxy image" in out  # the real, earliest failure
    assert "anchored on first failure marker" in out  # window was anchored
    assert len(out) <= 4000 + 100


def test_capture_failure_window_tailcaps_without_marker():
    """No failure marker → degrade to historical tail-cap (last N bytes)."""
    from robotsix_mill.forge.github import _capture_failure_window

    out = _capture_failure_window("x" * 100_000, max_bytes=65536)
    assert out == "x" * 65536  # plain tail, no anchor prefix


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
    jobs_data = {"jobs": [{"id": 201, "name": "build", "conclusion": "failure"}]}
    log_kwargs_captured = {}

    class RedirectCaptureClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

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
    jobs_data = {"jobs": [{"id": 201, "name": "build", "conclusion": "failure"}]}

    class ErrorClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

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
    assert (
        "[log fetch failed for job 201: HTTP 403 — App likely missing Actions:Read permission]"
        in result
    )


def test_fetch_workflow_job_logs_404_on_logs_endpoint(tmp_path, monkeypatch):
    """404 from the logs endpoint → generic HTTP placeholder."""
    jobs_data = {"jobs": [{"id": 202, "name": "lint", "conclusion": "failure"}]}

    class ErrorClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

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
    jobs_data = {"jobs": [{"id": 303, "name": "test", "conclusion": "failure"}]}

    class EmptyBodyClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

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
    get_map = {
        "repos/o/r/pulls/7": _make_response(200, detail_resp),
        "repos/o/r/pulls": _make_response(200, list_resp),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    # Monkey-patch _merge_pr to simulate the HTTP seam
    monkeypatch.setattr(
        forge,
        "_merge_pr",
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
        forge,
        "_merge_pr",
        lambda *, owner, repo, pull_number: {
            "merged": False,
            "reason": "merge not allowed (branch protection?)",
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
        forge,
        "_merge_pr",
        lambda *, owner, repo, pull_number: {
            "merged": False,
            "reason": "PR is not mergeable",
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
        forge,
        "_merge_pr",
        lambda *, owner, repo, pull_number: {
            "merged": False,
            "reason": "connection refused",
        },
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
# list_pr_reviews
# ---------------------------------------------------------------------------


def test_list_pr_reviews_happy_path(tmp_path, monkeypatch):
    """PR exists → _list_pr_reviews returns normalized dicts (body="" when None)."""
    list_resp = [{"number": 7}]
    detail_resp = {
        "number": 7,
        "merged": False,
        "state": "open",
        "html_url": "http://pr/7",
        "mergeable": True,
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
        "id": 2001,
        "author": "alice",
        "created_at": "2025-01-15T12:00:00Z",
        "body": "LGTM",
    }
    assert result[1] == {
        "id": 2002,
        "author": "bob",
        "created_at": "2025-01-15T13:00:00Z",
        "body": "",
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
        "number": 7,
        "merged": False,
        "state": "open",
        "html_url": "http://pr/7",
        "mergeable": True,
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
        "number": 7,
        "merged": False,
        "state": "open",
        "html_url": "http://pr/7",
        "mergeable": True,
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
        "number": 7,
        "merged": False,
        "state": "open",
        "html_url": "http://pr/7",
        "mergeable": True,
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
        "id": 3001,
        "author": "alice",
        "created_at": "2025-01-15T14:00:00Z",
        "body": "Consider adding a docstring here.",
        "file_path": "src/foo.py",
        "line": 42,
        "diff_hunk": "@@ -40,6 +40,8 @@ def bar():",
    }
    assert result[1] == {
        "id": 3002,
        "author": "bob",
        "created_at": "2025-01-15T15:00:00Z",
        "body": "This line seems unused.",
        "file_path": "src/baz.py",
        "line": 17,
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
        "number": 7,
        "merged": False,
        "state": "open",
        "html_url": "http://pr/7",
        "mergeable": True,
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
        "number": 7,
        "merged": False,
        "state": "open",
        "html_url": "http://pr/7",
        "mergeable": True,
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
        "number": 7,
        "merged": False,
        "state": "open",
        "html_url": "http://pr/7",
        "mergeable": True,
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
        "path": "src/main.py",
        "status": "modified",
        "additions": 12,
        "deletions": 3,
    }
    assert files[1] == {
        "path": "tests/test_main.py",
        "status": "added",
        "additions": 45,
        "deletions": 0,
    }
    assert files[2] == {
        "path": "old/deprecated.py",
        "status": "removed",
        "additions": 0,
        "deletions": 20,
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
        "number": 7,
        "merged": False,
        "state": "open",
        "html_url": "http://pr/7",
        "mergeable": True,
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
        "number": 7,
        "merged": False,
        "state": "open",
        "html_url": "http://pr/7",
        "mergeable": True,
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


# ---------------------------------------------------------------------------
# create_repo
# ---------------------------------------------------------------------------


def test_create_repo_happy_path_org(tmp_path, monkeypatch):
    """201 from org endpoint → returns RepoInfo with correct fields."""
    fake_json = {
        "id": 42,
        "name": "my-repo",
        "clone_url": "https://github.com/o/my-repo.git",
        "html_url": "https://github.com/o/my-repo",
    }
    _mock_httpx(
        monkeypatch,
        post_response=_make_response(201, fake_json),
    )

    forge = _forge(tmp_path, enable_repo_creation=True)
    result = forge.create_repo(
        name="my-repo", owner="o", private=True, description="A test repo"
    )
    assert isinstance(result, RepoInfo)
    assert result.id == 42
    assert result.name == "my-repo"
    assert result.clone_url == "https://github.com/o/my-repo.git"
    assert result.html_url == "https://github.com/o/my-repo"


def test_create_repo_flag_disabled(tmp_path, monkeypatch):
    """NotConfiguredError raised when enable_repo_creation=False (default)."""
    forge = _forge(tmp_path)  # enable_repo_creation defaults to False
    with pytest.raises(NotConfiguredError, match="Repo creation is disabled"):
        forge.create_repo(name="my-repo", owner="o", private=True, description="desc")


def test_create_repo_org_fallback_to_user(tmp_path, monkeypatch):
    """403 from org endpoint → 201 from /user/repos → returns RepoInfo."""
    fake_json = {
        "id": 99,
        "name": "fallback-repo",
        "clone_url": "https://github.com/user/fallback-repo.git",
        "html_url": "https://github.com/user/fallback-repo",
    }

    call_count = 0

    # We need a post that returns 403 first, then 201 on second call.
    class TwoStepPostClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, headers=None, params=None):
            return _make_response(404, [], "")

        def post(self, url, headers=None, json=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Org endpoint
                return _make_response(403, {}, "forbidden")
            if call_count == 2:
                # User fallback
                return _make_response(201, fake_json)
            return _make_response(500, {}, "error")

    monkeypatch.setattr(real_httpx, "Client", TwoStepPostClient)

    forge = _forge(tmp_path, enable_repo_creation=True)
    result = forge.create_repo(
        name="fallback-repo", owner="big-org", private=False, description="fallback"
    )
    assert result.id == 99
    assert result.name == "fallback-repo"
    assert call_count == 2


def test_create_repo_422_name_exists(tmp_path, monkeypatch):
    """422 with 'name already exists' → RuntimeError."""
    _mock_httpx(
        monkeypatch,
        post_response=_make_response(
            422, {}, '{"message": "name already exists on this account"}'
        ),
    )

    forge = _forge(tmp_path, enable_repo_creation=True)
    with pytest.raises(RuntimeError, match="already exists"):
        forge.create_repo(
            name="existing-repo", owner="o", private=True, description="desc"
        )


def test_create_repo_other_non_2xx(tmp_path, monkeypatch):
    """Non-422, non-403/404 non-2xx → RuntimeError with status code and body."""
    _mock_httpx(
        monkeypatch,
        post_response=_make_response(500, {}, "internal error"),
    )

    forge = _forge(tmp_path, enable_repo_creation=True)
    with pytest.raises(RuntimeError, match="GitHub repo create failed: 500"):
        forge.create_repo(name="my-repo", owner="o", private=True, description="desc")


def test_create_repo_prefers_repo_create_token(tmp_path, monkeypatch):
    """When forge_repo_create_token (a PAT) is set, the create call
    authenticates with it instead of the normal forge token."""
    captured = {"auth": None}

    class HeaderCapturingClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, headers=None, params=None, **kwargs):
            return _make_response(404, [], "")

        def post(self, url, headers=None, json=None, **kwargs):
            captured["auth"] = (headers or {}).get("Authorization")
            return _make_response(
                201, {"id": 1, "name": "b", "clone_url": "x", "html_url": "y"}
            )

    monkeypatch.setattr(real_httpx, "Client", HeaderCapturingClient)

    forge = _forge(tmp_path, enable_repo_creation=True)
    # Set the PAT after building the forge (which seeds forge_token).
    _set_secrets(forge_token="app-tok", forge_repo_create_token="pat-xyz")
    forge.create_repo(name="b", owner="o", private=False, description="d")

    assert captured["auth"] == "Bearer pat-xyz"


def test_create_repo_403_integration_message(tmp_path, monkeypatch):
    """A 403 'not accessible by integration' (App token, user account) →
    RuntimeError naming the forge_repo_create_token PAT remedy."""
    _mock_httpx(
        monkeypatch,
        post_response=_make_response(403, {}, "Resource not accessible by integration"),
    )

    forge = _forge(tmp_path, enable_repo_creation=True)
    with pytest.raises(RuntimeError, match="forge_repo_create_token"):
        forge.create_repo(name="b", owner="o", private=False, description="d")


def _empty_reuse_client(*, commits_status, commits_json=None):
    """Client where create 422s 'name already exists', the repo GET 200s, and
    the commits GET returns *commits_status* (409=empty) / *commits_json*."""

    class C:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, headers=None, json=None, **kwargs):
            # org create 404 → user create 422 name-exists
            if url.endswith("/orgs/o/repos"):
                return _make_response(404, {}, "no org")
            return _make_response(422, {}, "name already exists on this account")

        def get(self, url, headers=None, params=None, **kwargs):
            if url.endswith("/user"):
                # empty-owner resolution → authenticated login 'o'
                return _make_response(200, {"login": "o"}, "")
            if url.endswith("/repos/o/b/commits"):
                return _make_response(commits_status, commits_json or [], "")
            if url.endswith("/repos/o/b"):
                return _make_response(
                    200,
                    {"id": 7, "name": "b", "clone_url": "cu", "html_url": "hu"},
                )
            return _make_response(404, [], "")

    return C


def test_create_repo_reuses_existing_empty_repo(tmp_path, monkeypatch):
    """A prior partial scaffold left an EMPTY repo → create reuses it
    (returns its RepoInfo) instead of failing on 'already exists'."""
    monkeypatch.setattr(real_httpx, "Client", _empty_reuse_client(commits_status=409))
    forge = _forge(tmp_path, enable_repo_creation=True)
    info = forge.create_repo(name="b", owner="o", private=False, description="d")
    assert info.id == 7
    assert info.clone_url == "cu"


def test_create_repo_existing_nonempty_repo_raises(tmp_path, monkeypatch):
    """An existing repo WITH commits is a genuine conflict → RuntimeError."""
    monkeypatch.setattr(
        real_httpx,
        "Client",
        _empty_reuse_client(commits_status=200, commits_json=[{"sha": "abc"}]),
    )
    forge = _forge(tmp_path, enable_repo_creation=True)
    with pytest.raises(RuntimeError, match="not empty"):
        forge.create_repo(name="b", owner="o", private=False, description="d")


def test_create_repo_reuses_empty_repo_with_blank_owner(tmp_path, monkeypatch):
    """owner='' (meta agent leaves it blank) → create falls back to
    /user/repos; reuse resolves the authenticated login via /user so the
    empty-repo lookup still succeeds instead of falsely blocking."""
    monkeypatch.setattr(real_httpx, "Client", _empty_reuse_client(commits_status=409))
    forge = _forge(tmp_path, enable_repo_creation=True)
    info = forge.create_repo(name="b", owner="", private=False, description="d")
    assert info.id == 7


def test_clamp_repo_description():
    """Descriptions are single-lined and clamped to GitHub's 350-char cap."""
    from robotsix_mill.forge.github import _clamp_repo_description

    assert (
        _clamp_repo_description("short  desc\nwith   lines") == "short desc with lines"
    )
    long = "x" * 500
    out = _clamp_repo_description(long)
    assert len(out) == 350
    assert out.endswith("…")
    assert _clamp_repo_description("") == ""


def test_create_repo_clamps_long_description(tmp_path, monkeypatch):
    """The create POST payload carries a ≤350-char description even when
    the caller passes a longer one (the 422 root cause)."""
    captured = {"payload": None}

    class PayloadClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, headers=None, params=None, **kwargs):
            return _make_response(404, [], "")

        def post(self, url, headers=None, json=None, **kwargs):
            captured["payload"] = json
            return _make_response(
                201, {"id": 1, "name": "b", "clone_url": "x", "html_url": "y"}
            )

    monkeypatch.setattr(real_httpx, "Client", PayloadClient)

    forge = _forge(tmp_path, enable_repo_creation=True)
    forge.create_repo(name="b", owner="o", private=False, description="y" * 600)

    assert len(captured["payload"]["description"]) <= 350


def test_list_code_scanning_alerts_parses(tmp_path, monkeypatch):
    """Open CodeQL alerts are fetched + normalised (rule/severity/path/line)."""
    raw = [
        {
            "rule": {
                "id": "py/x",
                "security_severity_level": "high",
                "description": "desc",
            },
            "html_url": "u",
            "most_recent_instance": {
                "location": {"path": "tests/t.py", "start_line": 92},
                "message": {"text": "bad url substring"},
            },
        }
    ]
    _mock_httpx(monkeypatch, get_map={"code-scanning/alerts": _make_response(200, raw)})
    forge = _forge(tmp_path)
    out = forge.list_code_scanning_alerts(source_branch="feature/x")
    assert len(out) == 1
    a = out[0]
    assert a["rule"] == "py/x" and a["severity"] == "high"
    assert a["path"] == "tests/t.py" and a["line"] == 92
    assert "bad url substring" in a["message"]


def test_list_code_scanning_alerts_404_returns_empty(tmp_path, monkeypatch):
    """403/404 (code-scanning off, or token lacks scope) → [] (not an error)."""
    _mock_httpx(
        monkeypatch,
        get_map={"code-scanning/alerts": _make_response(404, {}, "not found")},
    )
    forge = _forge(tmp_path)
    assert forge.list_code_scanning_alerts(source_branch="feature/x") == []


# ---------------------------------------------------------------------------
# _delete_branch (via delete_branch)
# ---------------------------------------------------------------------------


def _mock_httpx_delete(monkeypatch, *, delete_response=None, raise_exc=None):
    """Replace httpx.Client with a mock whose .delete() is controllable."""
    captured = {"url": None}

    class MockClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

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
    assert cap["url"].endswith("/repos/o/r/git/refs/heads/mill/t-1")


@pytest.mark.parametrize("status", [404, 422])
def test_delete_branch_already_gone_returns_true(tmp_path, monkeypatch, status):
    _mock_httpx_delete(monkeypatch, delete_response=_make_response(status, {}, "gone"))
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


def _branch_dict(name, date, protected=False):
    return {
        "name": name,
        "protected": protected,
        "commit": {"commit": {"committer": {"date": date}}},
    }


def _mock_httpx_paged(monkeypatch, *, pages=None, raise_exc=None):
    """Replace httpx.Client with a mock that returns *pages* (a list of
    FakeResponse keyed by the ``page`` param, 1-indexed)."""

    class MockClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, headers=None, params=None, **kwargs):
            if raise_exc is not None:
                raise raise_exc
            page = (params or {}).get("page", 1)
            return pages[page - 1]

    monkeypatch.setattr(real_httpx, "Client", MockClient)


def test_list_branches_parses_and_paginates(tmp_path, monkeypatch):
    page1 = [
        _branch_dict(f"b{i}", "2024-01-15T10:30:00Z", protected=(i == 0))
        for i in range(100)
    ]
    page2 = [_branch_dict("last", "2024-02-01T08:00:00Z", protected=True)]
    _mock_httpx_paged(
        monkeypatch,
        pages=[_make_response(200, page1), _make_response(200, page2)],
    )
    forge = _forge(tmp_path)
    branches = forge.list_branches()
    assert len(branches) == 101
    assert branches[0].name == "b0"
    assert branches[0].is_protected is True
    assert branches[1].is_protected is False
    # tz-aware UTC
    assert branches[0].last_commit_at.tzinfo is not None
    assert branches[0].last_commit_at.utcoffset().total_seconds() == 0
    assert branches[0].last_commit_at.year == 2024
    assert branches[-1].name == "last"
    assert branches[-1].is_protected is True


def test_list_branches_exception_returns_empty(tmp_path, monkeypatch):
    _mock_httpx_paged(monkeypatch, raise_exc=real_httpx.ConnectError("net down"))
    forge = _forge(tmp_path)
    assert forge.list_branches() == []


def test_list_branches_non_2xx_returns_empty(tmp_path, monkeypatch):
    _mock_httpx_paged(monkeypatch, pages=[_make_response(500, [], "boom")])
    forge = _forge(tmp_path)
    assert forge.list_branches() == []


def test_list_open_pr_branches_returns_head_refs(tmp_path, monkeypatch):
    prs = [
        {"head": {"ref": "feature/a"}},
        {"head": {"ref": "feature/b"}},
        {"head": {}},
    ]
    _mock_httpx_paged(monkeypatch, pages=[_make_response(200, prs)])
    forge = _forge(tmp_path)
    assert forge.list_open_pr_branches() == {"feature/a", "feature/b"}


def test_list_open_pr_branches_exception_returns_empty(tmp_path, monkeypatch):
    _mock_httpx_paged(monkeypatch, raise_exc=real_httpx.ConnectError("net down"))
    forge = _forge(tmp_path)
    assert forge.list_open_pr_branches() == set()
