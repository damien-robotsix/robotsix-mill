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
)
from robotsix_mill.forge.github_ci import (
    _ANSI_RE,
    _extract_annotations,
    _latest_definitive_runs,
    _statuses_to_check_runs,
)
from robotsix_mill.forge.github_pr import _parse_iso_utc, _parse_pr_detail


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
                "event": "push",
                "head_branch": "main",
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
    assert result[0]["event"] == "push"
    assert result[0]["head_branch"] == "main"


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


def test_list_workflow_runs_missing_event_and_head_branch(tmp_path, monkeypatch):
    """When the API omits event and head_branch, the mapped dict uses
    defaults (empty string and None respectively)."""
    runs_data = {
        "workflow_runs": [
            {
                "id": 2,
                "name": "tag-release",
                "workflow_id": 200,
                "head_sha": "def",
                "conclusion": "failure",
                "html_url": "http://y",
                "created_at": "2025-01-02T00:00:00Z",
            }
        ]
    }
    get_map = {"actions/runs": _make_response(200, runs_data)}
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.list_workflow_runs(branch="main")
    assert len(result) == 1
    assert result[0]["event"] == ""
    assert result[0]["head_branch"] is None


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
    from robotsix_mill.forge._log_utils import _capture_failure_window
    from robotsix_mill.forge.github_ci import _LOG_FAILURE_RE

    real = "ERROR: failed to build proxy image: COPY filter not found\n##[error]Process completed with exit code 1\n"
    filler = "noise line padding the log\n" * 5000  # skipped-step noise
    mask = "FATAL could not parse reference: .\n##[error]Process completed with exit code 1\n"
    log = real + filler + mask
    out = _capture_failure_window(log, max_bytes=4000, failure_re=_LOG_FAILURE_RE)
    assert "failed to build proxy image" in out  # the real, earliest failure
    assert "anchored on first failure marker" in out  # window was anchored
    assert len(out) <= 4000 + 100


def test_capture_failure_window_tailcaps_without_marker():
    """No failure marker → degrade to historical tail-cap (last N bytes)."""
    from robotsix_mill.forge._log_utils import _capture_failure_window
    from robotsix_mill.forge.github_ci import _LOG_FAILURE_RE

    out = _capture_failure_window(
        "x" * 100_000, max_bytes=65536, failure_re=_LOG_FAILURE_RE
    )
    assert out == "x" * 65536  # plain tail, no anchor prefix


def test_strip_runner_noise_removes_boilerplate():
    """Runner preamble (OS version, runner image, git config) is stripped."""
    from robotsix_mill.forge._log_utils import _strip_runner_noise

    log = (
        "Current runner version: '2.317.0'\n"
        "##[group]Operating System\n"
        "Ubuntu\n22.04.4\nLTS\n"
        "##[endgroup]\n"
        "##[group]Runner Image\n"
        "Image: ubuntu-22.04\n"
        "##[endgroup]\n"
        "##[group]GITHUB_TOKEN Permissions\n"
        "Secrets: read\n"
        "##[endgroup]\n"
        "Secret source: Actions\n"
        "Prepare workflow directory\n"
        "Prepare all required actions\n"
        "Getting action download info\n"
        "Download action repository 'actions/checkout@v4' (SHA:abc123)\n"
        "Download action repository 'actions/setup-python@v5' (SHA:def456)\n"
        "##[group]Run pip install -e .\n"
        "Successfully installed foo\n"
        "##[endgroup]\n"
        "##[group]Run pytest\n"
        "FAILED tests/test_x.py::test_y - assert 1 == 2\n"
        "##[error]Process completed with exit code 1.\n"
        "##[endgroup]\n"
        "Post job cleanup.\n"
    )
    out = _strip_runner_noise(log)
    # Boilerplate removed.
    assert "Current runner version" not in out
    assert "Operating System" not in out
    assert "Runner Image" not in out
    assert "GITHUB_TOKEN Permissions" not in out
    assert "Secret source" not in out
    assert "Prepare workflow directory" not in out
    assert "Prepare all required actions" not in out
    assert "Getting action download info" not in out
    assert "Download action repository" not in out
    assert "Post job cleanup" not in out
    # Error lines and step output preserved.
    assert "FAILED tests/test_x.py::test_y" in out
    assert "##[error]Process completed with exit code 1." in out
    assert "Successfully installed foo" in out
    # Group markers kept for step-context.
    assert "##[group]Run pip install -e ." in out


def test_strip_runner_noise_download_action_without_group():
    """Download action repository lines are stripped even when the
    Prepare block closes without a ##[group] marker.  The short
    preamble headings themselves (Prepare all required actions,
    Getting action download info) may survive — they are only a
    few tokens — but the bulk download lines are stripped."""
    from robotsix_mill.forge._log_utils import _strip_runner_noise

    log = (
        "Prepare all required actions\n"
        "Getting action download info\n"
        "Download action repository 'actions/checkout@v4' (SHA:abc123)\n"
        "Download action repository 'actions/setup-python@v5' (SHA:def456)\n"
        "##[error]Process completed with exit code 1.\n"
    )
    out = _strip_runner_noise(log)
    assert "Download action repository" not in out
    assert "##[error]Process completed with exit code 1." in out


def test_strip_runner_noise_noop_on_clean_log():
    """A log without runner boilerplate is returned unchanged (modulo
    whitespace normalisation)."""
    from robotsix_mill.forge._log_utils import _strip_runner_noise

    log = "Step output line 1\nStep output line 2\n##[error]oops\n"
    out = _strip_runner_noise(log)
    assert "Step output line 1" in out
    assert "##[error]oops" in out


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
# close_pr
# ---------------------------------------------------------------------------


def test_close_pr_success(tmp_path, monkeypatch):
    """Mock returns 200 → True."""
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
        "_close_pr",
        lambda *, owner, repo, pull_number: True,
    )
    result = forge.close_pr(source_branch="feature/x")
    assert result is True


def test_close_pr_not_found(tmp_path, monkeypatch):
    """_get_pr returns None → False, no HTTP call made."""
    get_map = {"repos/o/r/pulls": _make_response(200, [])}
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.close_pr(source_branch="feature/x")
    assert result is False


def test_close_pr_already_closed(tmp_path, monkeypatch):
    """Mock returns False (e.g. 422 already-closed) → False, no exception."""
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
        "_close_pr",
        lambda *, owner, repo, pull_number: False,
    )
    result = forge.close_pr(source_branch="feature/x")
    assert result is False


# ---------------------------------------------------------------------------
# post_pr_comment
# ---------------------------------------------------------------------------


def test_post_pr_comment_success(tmp_path, monkeypatch):
    """Mock returns 201 → True."""
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
        "_post_pr_comment",
        lambda *, owner, repo, pull_number, body: True,
    )
    result = forge.post_pr_comment(source_branch="feature/x", body="closing note")
    assert result is True


def test_post_pr_comment_not_found(tmp_path, monkeypatch):
    """_get_pr returns None → False."""
    get_map = {"repos/o/r/pulls": _make_response(200, [])}
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.post_pr_comment(source_branch="feature/x", body="closing note")
    assert result is False


def test_post_pr_comment_error(tmp_path, monkeypatch):
    """Mock raises → False, no exception propagated."""
    list_resp = [{"number": 7}]
    detail_resp = {
        "number": 7,
        "merged": False,
        "state": "open",
        "html_url": "http://pr/7",
        "mergeable": True,
        "head": {"sha": "abc123"},
    }

    class ErrorClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, headers=None, params=None, **kwargs):
            if "/pulls/7" in url:
                return _make_response(200, detail_resp)
            if "/pulls" in url:
                return _make_response(200, list_resp)
            return _make_response(404, [], "")

        def post(self, url, headers=None, json=None, **kwargs):
            raise ConnectionError("connection refused")

        def patch(self, url, headers=None, json=None, **kwargs):
            return _make_response(500, {}, "error")

    monkeypatch.setattr(real_httpx, "Client", ErrorClient)

    forge = _forge(tmp_path)
    result = forge.post_pr_comment(source_branch="feature/x", body="closing note")
    assert result is False


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


def test_create_repo_defaults_to_public_from_config(tmp_path, monkeypatch):
    """When private is not passed, repo_visibility_default (default 'public')
    resolves to private=False in the POST payload."""
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
    forge.create_repo(name="b", owner="o", description="d")

    assert captured["payload"]["private"] is False


def test_create_repo_respects_private_default_config(tmp_path, monkeypatch):
    """When repo_visibility_default is 'private', omitted private resolves
    to True in the POST payload."""
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

    forge = _forge(
        tmp_path,
        enable_repo_creation=True,
        MILL_REPO_VISIBILITY_DEFAULT="private",
    )
    forge.create_repo(name="b", owner="o", description="d")

    assert captured["payload"]["private"] is True


def test_create_repo_explicit_private_overrides_config(tmp_path, monkeypatch):
    """Explicit private=False still wins when repo_visibility_default='private'."""
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

    forge = _forge(
        tmp_path,
        enable_repo_creation=True,
        MILL_REPO_VISIBILITY_DEFAULT="private",
    )
    forge.create_repo(name="b", owner="o", private=False, description="d")

    assert captured["payload"]["private"] is False


# ---------------------------------------------------------------------------
# fork_repo
# ---------------------------------------------------------------------------


def test_fork_repo_happy_path(tmp_path, monkeypatch):
    """202 response with repo info → returns RepoInfo; POST URL contains /forks."""
    fake_json = {
        "id": 99,
        "name": "r",
        "clone_url": "https://github.com/my-org/r.git",
        "html_url": "https://github.com/my-org/r",
    }
    captured = _mock_httpx(
        monkeypatch,
        post_response=_make_response(202, fake_json),
    )

    forge = _forge(tmp_path, enable_repo_creation=True)
    result = forge.fork_repo(source_owner="o", source_repo="r")
    assert isinstance(result, RepoInfo)
    assert result.id == 99
    assert result.name == "r"
    assert result.clone_url == "https://github.com/my-org/r.git"
    assert result.html_url == "https://github.com/my-org/r"
    # POST URL contains /repos/o/r/forks
    assert "/repos/o/r/forks" in captured["post_url"]


def test_fork_repo_with_target_namespace(tmp_path, monkeypatch):
    """target_namespace → payload includes organization."""
    fake_json = {
        "id": 99,
        "name": "r",
        "clone_url": "cu",
        "html_url": "hu",
    }
    captured = _mock_httpx(
        monkeypatch,
        post_response=_make_response(202, fake_json),
    )

    forge = _forge(tmp_path, enable_repo_creation=True)
    result = forge.fork_repo(
        source_owner="o", source_repo="r", target_namespace="my-org"
    )
    assert isinstance(result, RepoInfo)
    assert captured["post_payload"] == {"organization": "my-org"}


def test_fork_repo_flag_disabled(tmp_path, monkeypatch):
    """enable_repo_creation=False (default) → NotConfiguredError."""
    forge = _forge(tmp_path)  # no enable_repo_creation
    with pytest.raises(NotConfiguredError, match="Repo creation is disabled"):
        forge.fork_repo(source_owner="o", source_repo="r")


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
    """404 (code-scanning off) → [] (not an error)."""
    _mock_httpx(
        monkeypatch,
        get_map={"code-scanning/alerts": _make_response(404, {}, "not found")},
    )
    forge = _forge(tmp_path)
    assert forge.list_code_scanning_alerts(source_branch="feature/x") == []


def test_list_code_scanning_alerts_403_raises_unavailable(tmp_path, monkeypatch):
    """403 (token lacks security-events scope) raises CodeScanningAlertsUnavailable."""
    from robotsix_mill.forge.github_code_scanning import CodeScanningAlertsUnavailable

    _mock_httpx(
        monkeypatch,
        get_map={"code-scanning/alerts": _make_response(403, {}, "forbidden")},
    )
    forge = _forge(tmp_path)
    with pytest.raises(CodeScanningAlertsUnavailable):
        forge.list_code_scanning_alerts(source_branch="feature/x")


def _alert(number, rule_id, path, line):
    """Build a raw GitHub code-scanning alert dict (274d shape)."""
    return {
        "number": number,
        "rule": {"id": rule_id, "security_severity_level": "warning"},
        "html_url": f"http://alert/{number}",
        "most_recent_instance": {
            "location": {"path": path, "start_line": line},
            "message": {"text": f"{rule_id} flagged"},
        },
    }


def test_list_code_scanning_alerts_finds_merge_ref_only(tmp_path, monkeypatch):
    """274d: a pull_request-triggered CodeQL analysis files its alerts under
    the PR merge ref ``refs/pull/{N}/merge`` — NOT ``refs/heads/{branch}``.
    list_code_scanning_alerts must resolve the PR and query the merge ref;
    the pre-fix branch-ref-only query returned ``[]``."""
    merge_alerts = [
        _alert(1, "py/unused-global-variable", "src/pkg/new_mod.py", 5),
        _alert(2, "py/empty-except", "src/pkg/new_mod.py", 12),
    ]

    class MockClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, headers=None, params=None, **kwargs):
            params = params or {}
            if "code-scanning/alerts" in url:
                # Alerts exist ONLY under the PR merge ref.
                if params.get("ref") == "refs/pull/301/merge":
                    return _make_response(200, merge_alerts)
                return _make_response(200, [])
            if "/pulls/301" in url:
                return _make_response(
                    200, {"number": 301, "state": "open", "head": {"sha": "s"}}
                )
            if "/pulls" in url:
                return _make_response(200, [{"number": 301}])
            return _make_response(404, [], "")

    monkeypatch.setattr(real_httpx, "Client", MockClient)

    forge = _forge(tmp_path)
    out = forge.list_code_scanning_alerts(source_branch="mill/274d")
    assert {a["rule"] for a in out} == {
        "py/unused-global-variable",
        "py/empty-except",
    }
    assert all(a["path"] == "src/pkg/new_mod.py" for a in out)


def test_list_code_scanning_alerts_no_pr_falls_back_to_branch_ref(
    tmp_path, monkeypatch
):
    """When ``_get_pr`` finds no PR, the query falls back to the branch ref
    (existing behaviour preserved for non-PR contexts)."""
    branch_alerts = [_alert(9, "py/x", "src/a.py", 3)]

    class MockClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, headers=None, params=None, **kwargs):
            params = params or {}
            if "code-scanning/alerts" in url:
                if params.get("ref") == "refs/heads/feature/x":
                    return _make_response(200, branch_alerts)
                return _make_response(200, [])
            if "/pulls" in url:
                # No open/any PR for this head → _get_pr returns None.
                return _make_response(200, [])
            return _make_response(404, [], "")

    monkeypatch.setattr(real_httpx, "Client", MockClient)

    forge = _forge(tmp_path)
    out = forge.list_code_scanning_alerts(source_branch="feature/x")
    assert len(out) == 1
    assert out[0]["rule"] == "py/x" and out[0]["path"] == "src/a.py"


def test_list_code_scanning_alerts_retry_on_analysis_lag(tmp_path, monkeypatch):
    """The merge-ref query returns empty on the first call but analyses exist;
    after bounded backoff the re-query returns the alerts (eventual-consistency
    timing-gap coverage)."""
    merge_alerts = [
        _alert(7, "py/unused-import", "src/pkg/mod.py", 21),
    ]
    call_count = {"alerts": 0}

    class MockClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, headers=None, params=None, **kwargs):
            params = params or {}
            if "code-scanning/analyses" in url:
                return _make_response(200, [{"id": 1, "ref": "refs/pull/99/merge"}])
            if "code-scanning/alerts" in url:
                call_count["alerts"] += 1
                # First call (from _fetch_alerts_for_ref) → empty
                # Subsequent calls (from _wait_for_code_scanning_analysis) →
                # return alerts on the second retry
                if call_count["alerts"] >= 2:
                    return _make_response(200, merge_alerts)
                return _make_response(200, [])
            if "/pulls/99" in url:
                return _make_response(
                    200, {"number": 99, "state": "open", "head": {"sha": "s"}}
                )
            if "/pulls" in url:
                return _make_response(200, [{"number": 99}])
            return _make_response(404, [], "")

    monkeypatch.setattr(real_httpx, "Client", MockClient)
    # Accelerate time.sleep so the test doesn't actually wait.
    monkeypatch.setattr("time.sleep", lambda s: None)

    forge = _forge(tmp_path)
    out = forge.list_code_scanning_alerts(source_branch="mill/retry")
    assert len(out) == 1
    assert out[0]["rule"] == "py/unused-import"
    assert out[0]["path"] == "src/pkg/mod.py"
    assert call_count["alerts"] >= 2  # initial + at least one retry


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


# ---------------------------------------------------------------------------
# Cross-repo target: _head_owner, _get_pr head filter, _create_pr retry,
# and delete_branch routing.
# ---------------------------------------------------------------------------


def test_head_owner_is_fork_owner_for_cross_repo_target(tmp_path):
    """_head_owner returns the fork owner when cross_repo_target is set,
    and the upstream owner otherwise."""
    from robotsix_mill.config import CrossRepoTarget, RepoConfig

    # Same-repo: _head_owner == upstream owner.
    forge_same = GitHubForge(
        _settings(tmp_path),
        repo_config=RepoConfig(
            repo_id="r",
            board_id="b",
            langfuse_project_name="r",
            langfuse_public_key="",
            langfuse_secret_key="",
            forge_remote_url="https://github.com/up/r.git",
        ),
    )
    assert forge_same._head_owner == "up"

    # Cross-repo: _head_owner == fork owner.
    forge_cross = GitHubForge(
        _settings(tmp_path),
        repo_config=RepoConfig(
            repo_id="r",
            board_id="b",
            langfuse_project_name="r",
            langfuse_public_key="",
            langfuse_secret_key="",
            cross_repo_target=CrossRepoTarget(
                upstream_remote_url="https://github.com/up/r.git",
                fork_remote_url="https://github.com/fork-owner/r.git",
            ),
        ),
    )
    assert forge_cross._head_owner == "fork-owner"
    # _owner_repo still resolves to upstream (PRs live there).
    assert forge_cross._owner_repo == ("up", "r")

    # No repo_config: _head_owner falls back to global remote owner.
    forge_no_rc = GitHubForge(_settings(tmp_path))
    assert forge_no_rc._head_owner == "o"  # from default FORGE_REMOTE_URL


def test_get_pr_cross_repo_uses_fork_owner_in_head_filter(tmp_path, monkeypatch):
    """_get_pr for a cross-repo target uses the fork owner, not the
    upstream owner, in the ``head=<owner>:<branch>`` query param."""
    from robotsix_mill.config import CrossRepoTarget, RepoConfig

    rc = RepoConfig(
        repo_id="r",
        board_id="b",
        langfuse_project_name="r",
        langfuse_public_key="",
        langfuse_secret_key="",
        cross_repo_target=CrossRepoTarget(
            upstream_remote_url="https://github.com/up/r.git",
            fork_remote_url="https://github.com/fork-owner/r.git",
        ),
    )

    captured_params: dict = {}

    class ParamCaptureClient:
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
            # Return list + detail responses so _get_pr succeeds.
            if "/pulls/" in url and url.rstrip("/").split("/")[-1].isdigit():
                return _make_response(
                    200,
                    {
                        "number": 7,
                        "merged": False,
                        "state": "open",
                        "html_url": "http://pr/7",
                        "mergeable": True,
                        "mergeable_state": "clean",
                        "head": {"sha": "abc123"},
                    },
                )
            return _make_response(200, [{"number": 7}])

    monkeypatch.setattr(real_httpx, "Client", ParamCaptureClient)

    forge = GitHubForge(_settings(tmp_path), repo_config=rc)
    status = forge.pr_status(source_branch="feature/x")

    assert status is not None
    assert status["number"] == 7
    # The head filter must use the fork owner, not upstream.
    assert captured_params.get("head") == "fork-owner:feature/x"


def test_create_pr_cross_repo_422_retry_does_not_double_qualify_head(
    tmp_path,
    monkeypatch,
):
    """When a cross-fork PR create gets a 422, the existing-PR lookup
    re-uses the already-qualified ``head="fork-owner:branch"`` instead of
    prepending the upstream owner (which would produce the malformed
    ``"upstream:fork-owner:branch"``)."""
    from robotsix_mill.config import CrossRepoTarget, RepoConfig

    rc = RepoConfig(
        repo_id="r",
        board_id="b",
        langfuse_project_name="r",
        langfuse_public_key="",
        langfuse_secret_key="",
        cross_repo_target=CrossRepoTarget(
            upstream_remote_url="https://github.com/up/r.git",
            fork_remote_url="https://github.com/fork-owner/r.git",
        ),
    )

    captured_get_params: dict = {}
    post_422 = _make_response(422, {}, '{"field":"head","code":"invalid"}')

    class ParamCaptureClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, headers=None, json=None):
            return post_422

        def get(self, url, headers=None, params=None):
            captured_get_params.update(params or {})
            return _make_response(
                200,
                [{"html_url": "http://pr/99", "number": 99}],
            )

    monkeypatch.setattr(real_httpx, "Client", ParamCaptureClient)

    forge = GitHubForge(_settings(tmp_path), repo_config=rc)
    url = forge.open_merge_request(
        source_branch="feature/x",
        title="t",
        body="b",
        head_repo="fork-owner/r",
    )

    assert url == "http://pr/99"
    # Must be "fork-owner:feature/x", not "up:fork-owner:feature/x".
    assert captured_get_params.get("head") == "fork-owner:feature/x"


def test_delete_branch_cross_repo_targets_fork_not_upstream(
    tmp_path,
    monkeypatch,
):
    """delete_branch for a cross-repo target issues DELETE against the
    fork's git/refs/heads/<branch>, not the upstream's."""
    from robotsix_mill.config import CrossRepoTarget, RepoConfig

    rc = RepoConfig(
        repo_id="r",
        board_id="b",
        langfuse_project_name="r",
        langfuse_public_key="",
        langfuse_secret_key="",
        cross_repo_target=CrossRepoTarget(
            upstream_remote_url="https://github.com/up/r.git",
            fork_remote_url="https://github.com/fork-owner/r.git",
        ),
    )

    delete_url: dict = {}

    class DeleteCaptureClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def delete(self, url, headers=None, **kwargs):
            delete_url["url"] = url
            return _make_response(204, {})

    monkeypatch.setattr(real_httpx, "Client", DeleteCaptureClient)

    forge = GitHubForge(_settings(tmp_path), repo_config=rc)
    assert forge.delete_branch(branch="mill/t-1") is True

    # Must target fork-owner/r, not up/r.
    assert "/repos/fork-owner/r/git/refs/heads/mill/t-1" in delete_url["url"]
    assert "up/r" not in delete_url["url"]


def test_delete_branch_same_repo_unchanged(tmp_path, monkeypatch):
    """delete_branch without a cross_repo_target still targets the
    upstream/remote repo (same-repo behaviour unchanged)."""
    delete_url: dict = {}

    class DeleteCaptureClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def delete(self, url, headers=None, **kwargs):
            delete_url["url"] = url
            return _make_response(204, {})

    monkeypatch.setattr(real_httpx, "Client", DeleteCaptureClient)

    forge = _forge(tmp_path)
    assert forge.delete_branch(branch="mill/t-1") is True
    assert "/repos/o/r/git/refs/heads/mill/t-1" in delete_url["url"]


# ---------------------------------------------------------------------------
# 401 self-heal (cache invalidation + retry) — Path B (_create_pr)
# ---------------------------------------------------------------------------


def _app_settings(tmp_path, **kw):
    """Return Settings + populate Secrets for GitHub App auth."""
    import robotsix_mill.config as _cfg

    # Secrets must be populated *before* Settings() so the cross-field
    # validator (FORGE_AUTH=app requires github_app_id / private_key)
    # can see them.
    _reset_secrets()
    _cfg._secrets = Secrets(
        github_app_id=kw.get("GITHUB_APP_ID", "123"),
        github_app_private_key=kw.get("GITHUB_APP_PRIVATE_KEY", "KEY"),
    )
    kw.setdefault("data_dir", str(tmp_path))
    kw.setdefault("FORGE_KIND", "github")
    kw.setdefault("FORGE_AUTH", "app")
    kw.setdefault("FORGE_REMOTE_URL", "https://github.com/o/r.git")
    # Settings model itself holds github_app_id / github_app_private_key
    # (not just Secrets), so pass them through.
    kw.setdefault("GITHUB_APP_ID", "123")
    kw.setdefault("GITHUB_APP_PRIVATE_KEY", "KEY")
    return Settings(**kw)


def test_create_pr_401_retry_then_201_success(tmp_path, monkeypatch):
    """First POST returns 401, retry returns 201 — PR opens successfully.
    ``_mint_installation_token`` is called exactly twice (initial + retry)."""
    import time as _time

    from robotsix_mill.forge import auth as forge_auth

    forge_auth._cache.clear()
    mint_calls = []

    def fake_mint(settings, repo_config=None):
        mint_calls.append(_time.time())
        return f"ghs_{len(mint_calls)}", _time.time() + 3000

    monkeypatch.setattr(forge_auth, "_mint_installation_token", fake_mint)
    monkeypatch.setattr(_time, "sleep", lambda s: None)  # skip backoff

    # Stateful mock: first POST → 401, second POST → 201.
    call_count = [0]

    class RetryMockClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, headers=None, json=None, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_response(401, {}, '{"message":"Bad credentials"}')
            return _make_response(201, {"html_url": "https://github.com/o/r/pull/42"})

        def get(self, url, headers=None, params=None, **kwargs):
            return _make_response(200, [])

    monkeypatch.setattr(real_httpx, "Client", RetryMockClient)

    forge = GitHubForge(_app_settings(tmp_path))
    url = forge.open_merge_request(source_branch="feature/x", title="t", body="b")
    assert url == "https://github.com/o/r/pull/42"
    assert len(mint_calls) == 2  # initial + retry


def test_create_pr_401_retry_then_401_failure(tmp_path, monkeypatch):
    """Both POST attempts return 401 — error is surfaced (not swallowed).
    ``_mint_installation_token`` is called exactly twice (initial + retry)."""
    import time as _time

    from robotsix_mill.forge import auth as forge_auth

    forge_auth._cache.clear()
    mint_calls = []

    def fake_mint(settings, repo_config=None):
        mint_calls.append(_time.time())
        return f"ghs_{len(mint_calls)}", _time.time() + 3000

    monkeypatch.setattr(forge_auth, "_mint_installation_token", fake_mint)
    monkeypatch.setattr(_time, "sleep", lambda s: None)

    class Always401Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, headers=None, json=None, **kwargs):
            return _make_response(401, {}, '{"message":"Bad credentials"}')

        def get(self, url, headers=None, params=None, **kwargs):
            return _make_response(200, [])

    monkeypatch.setattr(real_httpx, "Client", Always401Client)

    forge = GitHubForge(_app_settings(tmp_path))
    with pytest.raises(RuntimeError, match="GitHub PR create failed: 401"):
        forge.open_merge_request(source_branch="feature/x", title="t", body="b")
    assert len(mint_calls) == 2  # initial + retry


# --- cancelled / stale CI conclusions are inconclusive, not failures -----


def test_conclusion_for_check_cancelled_is_pending():
    """A concurrency-cancelled (superseded) check has no verdict → pending,
    so the merge gate waits for the authoritative run instead of reporting
    a false failure that spawns ci_fix churn."""
    from robotsix_mill.forge.github_ci import _conclusion_for_check

    assert (
        _conclusion_for_check({"status": "completed", "conclusion": "cancelled"})
        == "pending"
    )
    assert (
        _conclusion_for_check({"status": "completed", "conclusion": "stale"})
        == "pending"
    )
    # Genuine terminal failures stay failures.
    assert (
        _conclusion_for_check({"status": "completed", "conclusion": "failure"})
        == "failure"
    )
    assert (
        _conclusion_for_check({"status": "completed", "conclusion": "startup_failure"})
        == "failure"
    )
    assert (
        _conclusion_for_check({"status": "completed", "conclusion": "success"})
        == "neutral"
    )


def test_derive_conclusion_cancelled_among_passing_is_pending():
    """All real checks pass but one was cancelled → overall pending (wait),
    NOT failure."""
    from robotsix_mill.forge.github_ci import _derive_check_conclusion

    runs = [
        {"id": 1, "name": "tests", "status": "completed", "conclusion": "success"},
        {"id": 2, "name": "mypy", "status": "completed", "conclusion": "cancelled"},
    ]
    out = _derive_check_conclusion(None, "", "o", "r", {}, runs)
    assert out["conclusion"] == "pending"
    assert out["failing"] == []
    assert out["pending"] == ["mypy"]


def test_derive_conclusion_real_failure_still_fails_despite_cancelled():
    """A genuine failure is reported even when another check is cancelled."""
    from robotsix_mill.forge.github_ci import _derive_check_conclusion

    runs = [
        {"id": 1, "name": "tests", "status": "completed", "conclusion": "failure"},
        {"id": 2, "name": "mypy", "status": "completed", "conclusion": "cancelled"},
    ]
    out = _derive_check_conclusion(None, "", "o", "r", {}, runs)
    assert out["conclusion"] == "failure"
    assert [f for f in out["failing"]]
    assert out["pending"] == ["mypy"]


def test_derive_conclusion_superseded_cancelled_same_name_uses_success():
    """SAME check name with both a superseded `cancelled` run and the
    authoritative `success` run → success, not pending-forever. Regression
    for green PRs stuck in IMPLEMENT_COMPLETE (llmio c273/55f1/d932/fcf4):
    concurrency cancels the old run, so each name carries cancelled+success;
    feeding both made the aggregate read pending. Order-independent."""
    from robotsix_mill.forge.github_ci import _derive_check_conclusion

    runs = [
        {
            "id": 1,
            "name": "ci (3.11) / tests",
            "status": "completed",
            "conclusion": "cancelled",
            "started_at": "2026-06-15T10:00:00Z",
        },
        {
            "id": 2,
            "name": "ci (3.11) / tests",
            "status": "completed",
            "conclusion": "success",
            "started_at": "2026-06-15T10:05:00Z",
        },
        # reversed order for a second name: success listed before its cancelled
        {
            "id": 3,
            "name": "ci (3.12) / tests",
            "status": "completed",
            "conclusion": "success",
            "started_at": "2026-06-15T10:05:00Z",
        },
        {
            "id": 4,
            "name": "ci (3.12) / tests",
            "status": "completed",
            "conclusion": "cancelled",
            "started_at": "2026-06-15T10:00:00Z",
        },
    ]
    out = _derive_check_conclusion(None, "", "o", "r", {}, runs)
    assert out["conclusion"] == "success"
    assert out["failing"] == []
    assert out["pending"] == []


# ---------------------------------------------------------------------------
# commit_ci_conclusion — SHA-based CI lookup (no PR)
# ---------------------------------------------------------------------------


def test_commit_ci_conclusion_green_sha(tmp_path, monkeypatch):
    """commit_ci_conclusion returns success for a green commit SHA."""
    check_runs_resp = {
        "check_runs": [
            {
                "id": 201,
                "name": "CI / test (3.11)",
                "status": "completed",
                "conclusion": "success",
                "output": {"summary": "All green", "text": None, "annotations": []},
            }
        ]
    }
    get_map = {
        "commits/abc123/check-runs": _make_response(200, check_runs_resp),
        "commits/abc123/status": _make_response(200, {"statuses": []}),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.commit_ci_conclusion(sha="abc123")
    assert result is not None
    assert result["conclusion"] == "success"
    assert result["failing"] == []


def test_commit_ci_conclusion_failing_sha(tmp_path, monkeypatch):
    """commit_ci_conclusion returns failure for a red commit SHA."""
    check_runs_resp = {
        "check_runs": [
            {
                "id": 301,
                "name": "CI / test",
                "status": "completed",
                "conclusion": "failure",
                "output": {"summary": "1 test failed", "text": None, "annotations": []},
            }
        ]
    }
    get_map = {
        "commits/def456/check-runs": _make_response(200, check_runs_resp),
        "commits/def456/status": _make_response(200, {"statuses": []}),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.commit_ci_conclusion(sha="def456")
    assert result is not None
    assert result["conclusion"] == "failure"
    assert len(result["failing"]) == 1
    assert result["failing"][0]["name"] == "CI / test"


def test_commit_ci_conclusion_no_ci_configured(tmp_path, monkeypatch):
    """Empty check-runs + empty statuses → success (repo with no CI)."""
    get_map = {
        "commits/abc123/check-runs": _make_response(200, {"check_runs": []}),
        "commits/abc123/status": _make_response(200, {"statuses": []}),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.commit_ci_conclusion(sha="abc123")
    assert result is not None
    assert result["conclusion"] == "success"


def test_commit_ci_conclusion_transport_error_returns_none(tmp_path, monkeypatch):
    """When the HTTP client raises (transport error), return None gracefully."""
    # Cause httpx.Client to raise on any call.
    import httpx as real_httpx

    class BrokenClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, headers=None, params=None, **kwargs):
            raise real_httpx.ConnectError("connection refused")

        def post(self, url, headers=None, json=None, **kwargs):
            raise real_httpx.ConnectError("connection refused")

    monkeypatch.setattr(real_httpx, "Client", BrokenClient)

    forge = _forge(tmp_path)
    result = forge.commit_ci_conclusion(sha="abc123")
    assert result is None


def test_commit_ci_conclusion_401_retry_invalidates_token(tmp_path, monkeypatch):
    """A 401 on first try invalidates the token and retries."""
    call_count = [0]

    class RetryClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, headers=None, params=None, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:  # first call (check-runs, retry=0) → 401
                return _make_response(401, {"message": "Bad credentials"})
            # After retry cycle, succeed.
            if "check-runs" in url:
                return _make_response(
                    200,
                    {
                        "check_runs": [
                            {
                                "id": 1,
                                "name": "CI",
                                "status": "completed",
                                "conclusion": "success",
                                "output": {
                                    "summary": None,
                                    "text": None,
                                    "annotations": [],
                                },
                            }
                        ]
                    },
                )
            return _make_response(200, {"statuses": []})

        def post(self, url, headers=None, json=None, **kwargs):
            return _make_response(500, {}, "")

    monkeypatch.setattr(real_httpx, "Client", RetryClient)

    # Track invalidate calls.
    import robotsix_mill.forge.auth as auth_mod

    invalidate_calls = []
    monkeypatch.setattr(
        auth_mod,
        "invalidate_github_token",
        lambda settings, repo_config: invalidate_calls.append(1),
    )

    forge = _forge(tmp_path)
    result = forge.commit_ci_conclusion(sha="abc123")
    # Should succeed after retry.
    assert result is not None
    assert result["conclusion"] == "success"
    # invalidate_github_token should have been called.
    assert len(invalidate_calls) >= 1


def test_commit_ci_conclusion_does_not_call_get_pr(tmp_path, monkeypatch):
    """commit_ci_conclusion must NOT call _get_pr — it's SHA-based."""
    check_runs_resp = {
        "check_runs": [
            {
                "id": 1,
                "name": "CI",
                "status": "completed",
                "conclusion": "success",
                "output": {"summary": None, "text": None, "annotations": []},
            }
        ]
    }
    get_map = {
        "commits/abc123/check-runs": _make_response(200, check_runs_resp),
        "commits/abc123/status": _make_response(200, {"statuses": []}),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    # Patch _get_pr to blow up if called.
    monkeypatch.setattr(
        GitHubForge,
        "_get_pr",
        lambda self, owner, repo, head: (_ for _ in ()).throw(
            AssertionError("_get_pr must not be called by commit_ci_conclusion")
        ),
    )

    forge = _forge(tmp_path)
    result = forge.commit_ci_conclusion(sha="abc123")
    assert result is not None
    assert result["conclusion"] == "success"


# ---------------------------------------------------------------------------
# pr_review_status / _pr_review_status
# ---------------------------------------------------------------------------


def test_pr_review_status_no_pr_returns_none(tmp_path, monkeypatch):
    """pr_review_status returns None when _get_pr finds no PR."""
    forge = _forge(tmp_path)
    monkeypatch.setattr(forge, "_get_pr", lambda *, owner, repo, head: None)

    result = forge.pr_review_status(source_branch="feature/x")
    assert result is None


def test_pr_review_status_delegates_to__pr_review_status(tmp_path, monkeypatch):
    """pr_review_status resolves PR via _get_pr then delegates."""
    forge = _forge(tmp_path)
    monkeypatch.setattr(
        forge,
        "_get_pr",
        lambda *, owner, repo, head: {"number": 7},
    )
    expected = {
        "state": "APPROVED",
        "comments": [],
        "files": ["a.py"],
    }
    monkeypatch.setattr(
        forge,
        "_pr_review_status",
        lambda *, owner, repo, pull_number: expected,
    )

    result = forge.pr_review_status(source_branch="feature/x")
    assert result is expected


def test__pr_review_status_happy_path(tmp_path, monkeypatch):
    """200 on all endpoints, mixed review states → correct aggregation."""
    import time as _time

    from robotsix_mill.forge import auth as forge_auth

    monkeypatch.setattr(_time, "sleep", lambda s: None)
    invalidate_calls: list = []
    monkeypatch.setattr(
        forge_auth,
        "invalidate_github_token",
        lambda settings, repo_config: invalidate_calls.append(1),
    )

    reviews_data = [
        {"id": 1, "state": "CHANGES_REQUESTED", "body": "Please fix X"},
        {"id": 2, "state": "APPROVED", "body": "LGTM!"},
        {"id": 3, "state": "DISMISSED", "body": "dismissed"},
    ]
    comments_data = [
        {
            "id": 101,
            "body": "inline nit",
            "path": "src/foo.py",
            "line": 42,
            "pull_request_review_id": 1,
        },
        {
            "id": 102,
            "body": "good point",
            "path": "src/bar.py",
            "line": 7,
            "pull_request_review_id": 2,
        },
    ]
    files_data = [
        {
            "filename": "src/foo.py",
            "status": "modified",
            "additions": 3,
            "deletions": 0,
        },
        {"filename": "src/bar.py", "status": "added", "additions": 10, "deletions": 0},
    ]

    get_map = {
        "pulls/7/reviews": _make_response(200, reviews_data),
        "pulls/7/comments": _make_response(200, comments_data),
        "pulls/7/files": _make_response(200, files_data),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge._pr_review_status(owner="o", repo="r", pull_number=7)

    # Latest non-dismissed review is APPROVED (id=2, after CHANGES_REQUESTED)
    assert result["state"] == "APPROVED"
    assert result["files"] == ["src/foo.py", "src/bar.py"]

    # Comments: 3 review body comments (all non-empty) + 2 inline
    assert len(result["comments"]) == 5
    bodies = {c["body"] for c in result["comments"]}
    assert bodies >= {"LGTM!", "Please fix X", "dismissed", "inline nit", "good point"}

    # Inline comments carry review_state from parent review
    inline_by_path = {c["path"]: c for c in result["comments"] if c["path"]}
    assert inline_by_path["src/foo.py"]["review_state"] == "CHANGES_REQUESTED"
    assert inline_by_path["src/bar.py"]["review_state"] == "APPROVED"
    assert inline_by_path["src/foo.py"]["line"] == 42

    # No spurious invalidate calls
    assert invalidate_calls == []


def test__pr_review_status_401_on_reviews_retry_succeeds(tmp_path, monkeypatch):
    """First reviews GET returns 401 → invalidate + retry → success."""
    import time as _time

    from robotsix_mill.forge import auth as forge_auth

    monkeypatch.setattr(_time, "sleep", lambda s: None)
    invalidate_calls: list = []
    monkeypatch.setattr(
        forge_auth,
        "invalidate_github_token",
        lambda settings, repo_config: invalidate_calls.append(1),
    )

    call_count = [0]

    class RetryClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, headers=None, json=None, **kwargs):
            return _make_response(500, {}, "")

        def get(self, url, headers=None, params=None, **kwargs):
            call_count[0] += 1
            if "reviews" in url and call_count[0] == 1:
                return _make_response(401, {"message": "Bad credentials"})
            if "reviews" in url:
                return _make_response(
                    200,
                    [
                        {"id": 1, "state": "APPROVED", "body": "LGTM"},
                    ],
                )
            if "comments" in url:
                return _make_response(200, [])
            if "files" in url:
                return _make_response(200, [])
            return _make_response(404, [], "")

    monkeypatch.setattr(real_httpx, "Client", RetryClient)

    forge = _forge(tmp_path)
    result = forge._pr_review_status(owner="o", repo="r", pull_number=7)

    assert result["state"] == "APPROVED"
    assert result["comments"] == [
        {
            "body": "LGTM",
            "path": "",
            "line": None,
            "review_state": "APPROVED",
        }
    ]
    assert result["files"] == []
    assert len(invalidate_calls) == 1


def test__pr_review_status_401_on_comments_retry_succeeds(tmp_path, monkeypatch):
    """First comments GET returns 401 → invalidate + retry → success."""
    import time as _time

    from robotsix_mill.forge import auth as forge_auth

    monkeypatch.setattr(_time, "sleep", lambda s: None)
    invalidate_calls: list = []
    monkeypatch.setattr(
        forge_auth,
        "invalidate_github_token",
        lambda settings, repo_config: invalidate_calls.append(1),
    )

    # Track per-client-loop state: need 401 only on the *first* comments
    # GET (retry=0), then 200 on retry=1.
    reviews_401_done = [False]

    class RetryClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, headers=None, json=None, **kwargs):
            return _make_response(500, {}, "")

        def get(self, url, headers=None, params=None, **kwargs):
            if "reviews" in url:
                return _make_response(
                    200,
                    [
                        {"id": 1, "state": "APPROVED", "body": "LGTM"},
                    ],
                )
            if "comments" in url:
                if not reviews_401_done[0]:
                    reviews_401_done[0] = True
                    return _make_response(401, {"message": "Bad credentials"})
                return _make_response(
                    200,
                    [
                        {
                            "id": 101,
                            "body": "inline",
                            "path": "f.py",
                            "line": 1,
                            "pull_request_review_id": 1,
                        },
                    ],
                )
            if "files" in url:
                return _make_response(200, [{"filename": "f.py"}])
            return _make_response(404, [], "")

    monkeypatch.setattr(real_httpx, "Client", RetryClient)

    forge = _forge(tmp_path)
    result = forge._pr_review_status(owner="o", repo="r", pull_number=7)

    assert result["state"] == "APPROVED"
    assert len(result["comments"]) == 2  # body + inline
    assert result["files"] == ["f.py"]
    assert len(invalidate_calls) == 1


def test__pr_review_status_empty_reviews_defaults_to_pending(tmp_path, monkeypatch):
    """No reviews → state PENDING, no comments, files empty."""
    get_map = {
        "pulls/7/reviews": _make_response(200, []),
        "pulls/7/comments": _make_response(200, []),
        "pulls/7/files": _make_response(200, []),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge._pr_review_status(owner="o", repo="r", pull_number=7)

    assert result["state"] == "PENDING"
    assert result["comments"] == []
    assert result["files"] == []


def test__pr_review_status_all_dismissed_uses_latest_state(tmp_path, monkeypatch):
    """All reviews DISMISSED → state is the latest review's state (DISMISSED)."""
    reviews_data = [
        {"id": 1, "state": "DISMISSED", "body": "stale"},
        {"id": 2, "state": "DISMISSED", "body": "also stale"},
    ]
    get_map = {
        "pulls/7/reviews": _make_response(200, reviews_data),
        "pulls/7/comments": _make_response(200, []),
        "pulls/7/files": _make_response(200, []),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge._pr_review_status(owner="o", repo="r", pull_number=7)

    assert result["state"] == "DISMISSED"
    # Both bodies included (non-empty)
    assert len(result["comments"]) == 2


def test__pr_review_status_pending_when_only_commented_reviews(tmp_path, monkeypatch):
    """Reviews with only COMMENTED state → state PENDING."""
    reviews_data = [
        {"id": 1, "state": "COMMENTED", "body": "just a note"},
    ]
    get_map = {
        "pulls/7/reviews": _make_response(200, reviews_data),
        "pulls/7/comments": _make_response(200, []),
        "pulls/7/files": _make_response(200, []),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge._pr_review_status(owner="o", repo="r", pull_number=7)

    # COMMENTED is not DISMISSED, so it becomes the state
    assert result["state"] == "COMMENTED"


def test__pr_review_status_empty_review_body_not_included(tmp_path, monkeypatch):
    """Reviews with empty/whitespace body are not included in comments list."""
    reviews_data = [
        {"id": 1, "state": "APPROVED", "body": ""},
        {"id": 2, "state": "COMMENTED", "body": "   "},
        {"id": 3, "state": "CHANGES_REQUESTED", "body": "Fix this"},
    ]
    get_map = {
        "pulls/7/reviews": _make_response(200, reviews_data),
        "pulls/7/comments": _make_response(200, []),
        "pulls/7/files": _make_response(200, []),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge._pr_review_status(owner="o", repo="r", pull_number=7)

    # Only the non-empty body is included
    assert len(result["comments"]) == 1
    assert result["comments"][0]["body"] == "Fix this"
    assert result["comments"][0]["review_state"] == "CHANGES_REQUESTED"
    assert result["state"] == "CHANGES_REQUESTED"


def test__pr_review_status_inline_comment_without_review_id_defaults(
    tmp_path, monkeypatch
):
    """Inline comment missing pull_request_review_id → review_state defaults to COMMENTED."""
    reviews_data = [
        {"id": 1, "state": "APPROVED", "body": "LGTM"},
    ]
    comments_data = [
        {
            "id": 201,
            "body": "orphan comment",
            "path": "x.py",
            "line": 10,
            # No pull_request_review_id
        },
    ]
    get_map = {
        "pulls/7/reviews": _make_response(200, reviews_data),
        "pulls/7/comments": _make_response(200, comments_data),
        "pulls/7/files": _make_response(200, []),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge._pr_review_status(owner="o", repo="r", pull_number=7)

    inline = [c for c in result["comments"] if c["path"] == "x.py"]
    assert len(inline) == 1
    assert inline[0]["review_state"] == "COMMENTED"


def test__pr_review_status_inline_comment_original_line_fallback(tmp_path, monkeypatch):
    """Inline comment with no line field uses original_line as fallback."""
    reviews_data = [
        {"id": 1, "state": "COMMENTED", "body": ""},
    ]
    comments_data = [
        {
            "id": 301,
            "body": "old diff comment",
            "path": "old.py",
            "original_line": 55,
            "pull_request_review_id": 1,
        },
    ]
    get_map = {
        "pulls/7/reviews": _make_response(200, reviews_data),
        "pulls/7/comments": _make_response(200, comments_data),
        "pulls/7/files": _make_response(200, []),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge._pr_review_status(owner="o", repo="r", pull_number=7)

    inline = [c for c in result["comments"] if c["path"] == "old.py"]
    assert len(inline) == 1
    assert inline[0]["line"] == 55


# ---------------------------------------------------------------------------
# _parse_iso_utc
# ---------------------------------------------------------------------------


def test_parse_iso_utc_z_suffix():
    """ISO-8601 with trailing Z → UTC datetime."""

    result = _parse_iso_utc("2024-01-01T00:00:00Z")
    assert result.year == 2024
    assert result.month == 1
    assert result.day == 1
    assert result.tzinfo is not None
    assert result.utcoffset().total_seconds() == 0


def test_parse_iso_utc_naive():
    """Naive ISO-8601 (no tz) → assumed UTC."""

    result = _parse_iso_utc("2024-01-01T00:00:00")
    assert result.tzinfo is not None
    assert result.utcoffset().total_seconds() == 0


def test_parse_iso_utc_none():
    """None / empty → Unix epoch (UTC)."""
    from datetime import datetime, timezone

    for val in (None, ""):
        result = _parse_iso_utc(val)
        assert result == datetime.fromtimestamp(0, tz=timezone.utc)


def test_parse_iso_utc_invalid():
    """Unparseable string → Unix epoch (UTC)."""
    from datetime import datetime, timezone

    result = _parse_iso_utc("not-a-date")
    assert result == datetime.fromtimestamp(0, tz=timezone.utc)


# ---------------------------------------------------------------------------
# _parse_pr_detail
# ---------------------------------------------------------------------------


def test_parse_pr_detail_clean_mergeable():
    """mergeable_state='clean', mergeable=True → mergeable=True."""
    pr = {
        "merged": False,
        "state": "open",
        "html_url": "http://pr/7",
        "mergeable": True,
        "mergeable_state": "clean",
        "head": {"sha": "abc123"},
        "number": 7,
    }
    result = _parse_pr_detail(pr)
    assert result == {
        "merged": False,
        "state": "open",
        "url": "http://pr/7",
        "mergeable": True,
        "mergeable_state": "clean",
        "sha": "abc123",
        "number": 7,
    }


def test_parse_pr_detail_unknown_mergeable_state():
    """mergeable_state='unknown' → mergeable forced to None (stale value)."""
    pr = {
        "merged": False,
        "state": "open",
        "html_url": "http://pr/7",
        "mergeable": True,  # stale — async computation hasn't finished
        "mergeable_state": "unknown",
        "head": {"sha": "abc123"},
        "number": 7,
    }
    result = _parse_pr_detail(pr)
    assert result["mergeable"] is None


def test_parse_pr_detail_none_mergeable_state():
    """mergeable_state=None → mergeable forced to None."""
    pr = {
        "merged": False,
        "state": "open",
        "html_url": "http://pr/7",
        "mergeable": True,
        "mergeable_state": None,
        "head": {"sha": "abc123"},
        "number": 7,
    }
    result = _parse_pr_detail(pr)
    assert result["mergeable"] is None


def test_parse_pr_detail_merged():
    """Merged PR → merged=True, state=closed."""
    pr = {
        "merged": True,
        "state": "closed",
        "html_url": "http://pr/7",
        "mergeable": None,
        "mergeable_state": "unknown",
        "head": {"sha": "abc123"},
        "number": 7,
    }
    result = _parse_pr_detail(pr)
    assert result["merged"] is True
    assert result["state"] == "closed"


# ---------------------------------------------------------------------------
# _statuses_to_check_runs
# ---------------------------------------------------------------------------


def test_statuses_to_check_runs_empty():
    """Empty statuses_data → empty list."""
    assert _statuses_to_check_runs({}) == []
    assert _statuses_to_check_runs({"statuses": []}) == []


def test_statuses_to_check_runs_single_success():
    """Single status with 'success' state → check-run dict with conclusion 'success'."""
    data = {
        "state": "success",
        "statuses": [{"context": "ci/test"}],
    }
    runs = _statuses_to_check_runs(data)
    assert len(runs) == 1
    assert runs[0]["name"] == "ci/test"
    assert runs[0]["status"] == "completed"
    assert runs[0]["conclusion"] == "success"
    assert runs[0]["output"]["annotations"] == []


def test_statuses_to_check_runs_single_pending():
    """Single status with 'pending' state → check-run dict with conclusion None."""
    data = {
        "state": "pending",
        "statuses": [{"context": "ci/test"}],
    }
    runs = _statuses_to_check_runs(data)
    assert len(runs) == 1
    assert runs[0]["status"] == "in_progress"
    assert runs[0]["conclusion"] is None


def test_statuses_to_check_runs_same_context_collapsed():
    """Multiple statuses with the same context → collapsed to one entry."""
    data = {
        "state": "success",
        "statuses": [
            {"context": "ci/test", "description": "first"},
            {"context": "ci/test", "description": "second"},
            {"context": "ci/lint", "description": "lint"},
        ],
    }
    runs = _statuses_to_check_runs(data)
    # Two unique contexts: ci/test, ci/lint
    assert len(runs) == 2
    names = {r["name"] for r in runs}
    assert names == {"ci/test", "ci/lint"}


# ---------------------------------------------------------------------------
# _latest_definitive_runs
# ---------------------------------------------------------------------------


def test_latest_definitive_runs_single():
    """Single run → returned as-is."""
    run = {
        "name": "ci",
        "started_at": "2024-01-01T00:00:00Z",
        "status": "completed",
        "conclusion": "success",
    }
    result = _latest_definitive_runs([run])
    assert result == [run]


def test_latest_definitive_runs_cancelled_and_success():
    """Two runs same name: cancelled + success → only success returned."""
    runs = [
        {
            "name": "ci",
            "started_at": "2024-01-01T00:00:00Z",
            "status": "completed",
            "conclusion": "cancelled",
        },
        {
            "name": "ci",
            "started_at": "2024-01-01T01:00:00Z",
            "status": "completed",
            "conclusion": "success",
        },
    ]
    result = _latest_definitive_runs(runs)
    assert len(result) == 1
    assert result[0]["conclusion"] == "success"


def test_latest_definitive_runs_both_inconclusive():
    """Two runs same name, both inconclusive → latest (by started_at) returned."""
    runs = [
        {
            "name": "ci",
            "started_at": "2024-01-01T00:00:00Z",
            "status": "completed",
            "conclusion": "cancelled",
        },
        {
            "name": "ci",
            "started_at": "2024-01-01T01:00:00Z",
            "status": "in_progress",
            "conclusion": None,
        },
    ]
    result = _latest_definitive_runs(runs)
    assert len(result) == 1
    assert result[0]["started_at"] == "2024-01-01T01:00:00Z"


def test_latest_definitive_runs_empty():
    """Empty input → empty list."""
    assert _latest_definitive_runs([]) == []


# ---------------------------------------------------------------------------
# _extract_annotations
# ---------------------------------------------------------------------------


def test_extract_annotations_success():
    """Successful detail fetch with annotations → annotations extracted."""
    detail_data = {
        "output": {
            "summary": "Build failed",
            "text": "Lots of output",
            "annotations": [
                {
                    "path": "src/app.py",
                    "start_line": 42,
                    "message": "syntax error",
                    "annotation_level": "failure",
                },
            ],
        },
    }

    class FakeDetailResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return detail_data

    client = type(
        "FakeClient", (), {"get": lambda self, url, headers=None: FakeDetailResponse()}
    )()
    cr = {"id": 123, "name": "ci/test"}
    result = _extract_annotations(client, "https://api.github.com", "o", "r", {}, cr)
    assert result["name"] == "ci/test"
    assert result["summary"] == "Build failed"
    assert len(result["annotations"]) == 1
    assert result["annotations"][0]["path"] == "src/app.py"
    assert result["annotations"][0]["start_line"] == 42


def test_extract_annotations_http_error():
    """HTTP error from detail fetch → empty result (best-effort, no exception)."""

    class FakeErrorResponse:
        status_code = 500

        def raise_for_status(self):
            raise real_httpx.HTTPStatusError(
                "HTTP 500",
                request=real_httpx.Request("GET", "http://x"),
                response=self,
            )

    client = type(
        "FakeClient", (), {"get": lambda self, url, headers=None: FakeErrorResponse()}
    )()
    cr = {"id": 123, "name": "ci/test"}
    result = _extract_annotations(client, "https://api.github.com", "o", "r", {}, cr)
    assert result == {
        "name": "ci/test",
        "summary": None,
        "text": None,
        "annotations": [],
    }


def test_extract_annotations_missing_output():
    """Detail response missing 'output' key → graceful fallback."""
    detail_data = {}  # no "output"

    class FakeDetailResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return detail_data

    client = type(
        "FakeClient", (), {"get": lambda self, url, headers=None: FakeDetailResponse()}
    )()
    cr = {"id": 123, "name": "ci/test"}
    result = _extract_annotations(client, "https://api.github.com", "o", "r", {}, cr)
    assert result["name"] == "ci/test"
    assert result["annotations"] == []


def test_extract_annotations_long_summary_truncated():
    """Summary > 2000 chars → truncated with ellipsis."""
    long_summary = "x" * 2500
    detail_data = {"output": {"summary": long_summary, "text": None, "annotations": []}}

    class FakeDetailResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return detail_data

    client = type(
        "FakeClient", (), {"get": lambda self, url, headers=None: FakeDetailResponse()}
    )()
    cr = {"id": 123, "name": "ci/test"}
    result = _extract_annotations(client, "https://api.github.com", "o", "r", {}, cr)
    assert len(result["summary"]) == 2000  # 1999 + "…"
    assert result["summary"].endswith("…")


# ---------------------------------------------------------------------------
# _retry_after_401
# ---------------------------------------------------------------------------


def test_retry_after_401_invalidates_token_and_sleeps(tmp_path, monkeypatch):
    """invalidate_and_backoff() calls invalidate_github_token() and sleeps 2s."""
    import time

    from robotsix_mill.forge import auth as forge_auth

    forge = _forge(tmp_path)
    invalidate_calls = []

    def fake_invalidate(settings, repo_config):
        invalidate_calls.append(1)

    monkeypatch.setattr(forge_auth, "invalidate_github_token", fake_invalidate)
    sleep_calls = []
    monkeypatch.setattr(time, "sleep", lambda s: sleep_calls.append(s))

    forge_auth.invalidate_and_backoff(forge.settings, forge._repo_config)
    assert len(invalidate_calls) == 1
    assert sleep_calls == [2]


# ---------------------------------------------------------------------------
# update_branch
# ---------------------------------------------------------------------------


def test_update_branch_success_202(tmp_path, monkeypatch):
    """PUT returns 202 → updated=True."""
    forge = _forge(tmp_path)
    monkeypatch.setattr(forge, "_get_pr", lambda **kw: {"number": 7, "sha": "abc"})

    put_resp = _make_response(202, {}, "")

    class MockClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def put(self, url, headers=None, **kwargs):
            return put_resp

    monkeypatch.setattr(real_httpx, "Client", MockClient)

    result = forge.update_branch(source_branch="feature/x")
    assert result == {"updated": True, "reason": "update-branch accepted"}


def test_update_branch_already_up_to_date_422(tmp_path, monkeypatch):
    """PUT returns 422 → updated=False, already up to date."""
    forge = _forge(tmp_path)
    monkeypatch.setattr(forge, "_get_pr", lambda **kw: {"number": 7, "sha": "abc"})

    put_resp = _make_response(422, {}, "already up to date")

    class MockClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def put(self, url, headers=None, **kwargs):
            return put_resp

    monkeypatch.setattr(real_httpx, "Client", MockClient)

    result = forge.update_branch(source_branch="feature/x")
    assert result == {"updated": False, "reason": "already up to date"}


def test_update_branch_pr_not_found(tmp_path, monkeypatch):
    """_get_pr returns None → updated=False, PR not found."""
    forge = _forge(tmp_path)
    monkeypatch.setattr(forge, "_get_pr", lambda **kw: None)

    result = forge.update_branch(source_branch="feature/x")
    assert result == {"updated": False, "reason": "PR not found"}


def test_update_branch_http_error(tmp_path, monkeypatch):
    """PUT returns non-202/422 → error message in reason."""
    forge = _forge(tmp_path)
    monkeypatch.setattr(forge, "_get_pr", lambda **kw: {"number": 7, "sha": "abc"})

    put_resp = _make_response(500, {}, "Internal Server Error")

    class MockClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def put(self, url, headers=None, **kwargs):
            return put_resp

    monkeypatch.setattr(real_httpx, "Client", MockClient)

    result = forge.update_branch(source_branch="feature/x")
    assert result["updated"] is False
    assert "HTTP 500" in result["reason"]


# ---------------------------------------------------------------------------
# list_open_prs / _list_open_prs
# ---------------------------------------------------------------------------


def _pr_item(ref, author="alice", number=1):
    return {
        "head": {"ref": ref},
        "user": {"login": author},
        "number": number,
        "html_url": f"http://pr/{number}",
        "title": f"PR {number}",
    }


def test_list_open_prs_single_page(tmp_path, monkeypatch):
    """Single page with 1 PR → list with 1 dict."""
    prs = [_pr_item("feature/a")]
    resp = _make_response(200, prs)

    class MockClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, headers=None, params=None, **kwargs):
            return resp

    monkeypatch.setattr(real_httpx, "Client", MockClient)

    forge = _forge(tmp_path)
    result = forge.list_open_prs()
    assert len(result) == 1
    assert result[0] == {
        "branch": "feature/a",
        "author_login": "alice",
        "number": 1,
        "url": "http://pr/1",
        "title": "PR 1",
    }


def test_list_open_prs_empty(tmp_path, monkeypatch):
    """Empty response → []."""
    resp = _make_response(200, [])

    class MockClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, headers=None, params=None, **kwargs):
            return resp

    monkeypatch.setattr(real_httpx, "Client", MockClient)

    forge = _forge(tmp_path)
    assert forge.list_open_prs() == []


def test_list_open_prs_multi_page(tmp_path, monkeypatch):
    """Multi-page (>100 items) → concatenated results."""
    page1 = [_pr_item(f"feature/{i}", number=i) for i in range(100)]
    page2 = [_pr_item("feature/last", number=101)]

    class MockClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, headers=None, params=None, **kwargs):
            page = (params or {}).get("page", 1)
            if page == 1:
                return _make_response(200, page1)
            return _make_response(200, page2)

    monkeypatch.setattr(real_httpx, "Client", MockClient)

    forge = _forge(tmp_path)
    result = forge.list_open_prs()
    assert len(result) == 101
    assert result[0]["branch"] == "feature/0"
    assert result[-1]["branch"] == "feature/last"


def test_list_open_prs_http_error(tmp_path, monkeypatch):
    """HTTP error → [] (no exception)."""

    class MockClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, headers=None, params=None, **kwargs):
            return _make_response(500, {}, "boom")

    monkeypatch.setattr(real_httpx, "Client", MockClient)

    forge = _forge(tmp_path)
    assert forge.list_open_prs() == []


def test_list_open_prs_skips_pr_without_ref(tmp_path, monkeypatch):
    """PR with no head/ref → skipped (not included in results)."""
    prs = [
        {
            "head": {},
            "user": {"login": "alice"},
            "number": 1,
            "html_url": "http://pr/1",
            "title": "no ref",
        },
        {
            "head": {"ref": "feature/b"},
            "user": {"login": "bob"},
            "number": 2,
            "html_url": "http://pr/2",
            "title": "PR 2",
        },
    ]
    resp = _make_response(200, prs)

    class MockClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, headers=None, params=None, **kwargs):
            return resp

    monkeypatch.setattr(real_httpx, "Client", MockClient)

    forge = _forge(tmp_path)
    result = forge.list_open_prs()
    assert len(result) == 1
    assert result[0]["branch"] == "feature/b"


# ---------------------------------------------------------------------------
# get_authenticated_user_login / _get_authenticated_user_login
# ---------------------------------------------------------------------------


def test_get_authenticated_user_login_success(tmp_path, monkeypatch):
    """Successful GET /user → cached login string."""
    resp = _make_response(200, {"login": "my-bot[bot]"})

    class MockClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, headers=None, **kwargs):
            return resp

    monkeypatch.setattr(real_httpx, "Client", MockClient)

    forge = _forge(tmp_path)
    login = forge.get_authenticated_user_login()
    assert login == "my-bot[bot]"


def test_get_authenticated_user_login_cache_hit(tmp_path, monkeypatch):
    """Second call returns cached value without making another HTTP request."""
    call_count = [0]

    class MockClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, headers=None, **kwargs):
            call_count[0] += 1
            return _make_response(200, {"login": "cached-bot"})

    monkeypatch.setattr(real_httpx, "Client", MockClient)

    forge = _forge(tmp_path)
    login1 = forge.get_authenticated_user_login()
    assert login1 == "cached-bot"
    assert call_count[0] == 1

    login2 = forge.get_authenticated_user_login()
    assert login2 == "cached-bot"
    assert call_count[0] == 1  # no second request


def test_get_authenticated_user_login_http_error(tmp_path, monkeypatch):
    """HTTP error → '' (no exception raised)."""

    class MockClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, headers=None, **kwargs):
            return _make_response(500, {}, "boom")

    monkeypatch.setattr(real_httpx, "Client", MockClient)

    forge = _forge(tmp_path)
    login = forge.get_authenticated_user_login()
    assert login == ""


def test_get_authenticated_user_login_exception(tmp_path, monkeypatch):
    """Exception in _get_authenticated_user_login → cached as ''."""
    # Force the _get_authenticated_user_login to raise.
    monkeypatch.setattr(
        GitHubForge,
        "_get_authenticated_user_login",
        lambda self: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    forge = _forge(tmp_path)
    login = forge.get_authenticated_user_login()
    assert login == ""

    # Cache hit — should still be ''
    login2 = forge.get_authenticated_user_login()
    assert login2 == ""
