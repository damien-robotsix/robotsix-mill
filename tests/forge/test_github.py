"""Test GitHubForge HTTP seams with mocked httpx.Client.

No stage-level monkeypatching — tests call _create_pr, _get_pr, _check_status,
_parse_owner_repo, and _build_headers directly with a mocked transport.
"""

import httpx as real_httpx
import pytest

from robotsix_mill.config import Secrets, Settings, _reset_secrets
from robotsix_mill.forge.base import NotConfiguredError, RepoInfo
from robotsix_mill.forge.github import (
    GitHubForge,
    _build_headers,
    _parse_owner_repo,
)
from robotsix_mill.forge.github_ci import (
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
    ("url", "expected"),
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
                "run_attempt": 2,
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
    assert result[0]["run_attempt"] == 2
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
    assert result[0]["run_attempt"] is None


def test_approve_workflow_success(tmp_path, monkeypatch):
    """POST .../actions/runs/11/approve → {"approved": True}."""
    captured = _mock_httpx(monkeypatch, post_response=_make_response(204, {}))
    forge = _forge(tmp_path)
    result = forge.approve_workflow(run_id=11)
    assert result == {"approved": True}
    assert "actions/runs/11/approve" in captured["post_url"]


def test_approve_workflow_403_flags_forbidden(tmp_path, monkeypatch):
    """A 403 approval refusal surfaces {"approved": False, "forbidden": True}."""
    _mock_httpx(monkeypatch, post_response=_make_response(403, {}, "forbidden"))
    forge = _forge(tmp_path)
    result = forge.approve_workflow(run_id=11)
    assert result["approved"] is False
    assert result["forbidden"] is True
    assert "403" in result["reason"]


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
    """Non-2xx from reviews endpoint → returns [] gracefully (paginated)."""
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
    assert forge.list_pr_reviews(source_branch="feature/x") == []


def test_list_pr_reviews_multi_page(tmp_path, monkeypatch):
    """More than 100 reviews → all pages are fetched (bug fix)."""
    list_resp = [{"number": 7}]
    detail_resp = {
        "number": 7,
        "merged": False,
        "state": "open",
        "html_url": "http://pr/7",
        "mergeable": True,
        "head": {"sha": "abc123"},
    }
    page1 = [
        {
            "id": i,
            "user": {"login": f"user{i}"},
            "submitted_at": "2025-01-15T12:00:00Z",
            "body": f"review {i}",
        }
        for i in range(100)
    ]
    page2 = [
        {
            "id": 200,
            "user": {"login": "last"},
            "submitted_at": "2025-01-16T12:00:00Z",
            "body": "final",
        }
    ]

    import httpx as real_httpx_module

    class MultiPageClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, headers=None, params=None, **kwargs):
            if "reviews" in url:
                page = (params or {}).get("page", 1)
                if page == 2:
                    return _make_response(200, page2)
                return _make_response(200, page1)
            if "repos/o/r/pulls/7" in url:
                return _make_response(200, detail_resp)
            if "repos/o/r/pulls" in url:
                return _make_response(200, list_resp)
            return _make_response(404, [], "")

    monkeypatch.setattr(real_httpx_module, "Client", MultiPageClient)

    forge = _forge(tmp_path)
    result = forge.list_pr_reviews(source_branch="feature/x")
    assert len(result) == 101
    assert result[0]["id"] == 0
    assert result[-1]["id"] == 200
    assert result[-1]["author"] == "last"


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
    """Non-2xx from review-comments endpoint → returns [] gracefully (paginated)."""
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
    assert forge.list_review_comments(source_branch="feature/x") == []


def test_list_review_comments_multi_page(tmp_path, monkeypatch):
    """More than 100 review comments → all pages are fetched (bug fix)."""
    list_resp = [{"number": 7}]
    detail_resp = {
        "number": 7,
        "merged": False,
        "state": "open",
        "html_url": "http://pr/7",
        "mergeable": True,
        "head": {"sha": "abc123"},
    }
    page1 = [
        {
            "id": i,
            "user": {"login": f"user{i}"},
            "created_at": "2025-01-15T12:00:00Z",
            "body": f"comment {i}",
            "path": f"src/file_{i}.py",
            "line": i,
            "diff_hunk": "@@ ... @@",
        }
        for i in range(100)
    ]
    page2 = [
        {
            "id": 200,
            "user": {"login": "last"},
            "created_at": "2025-01-16T12:00:00Z",
            "body": "final",
            "path": "src/last.py",
            "line": 42,
            "diff_hunk": "@@ ... @@",
        }
    ]

    import httpx as real_httpx_module

    class MultiPageClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, headers=None, params=None, **kwargs):
            if "comments" in url:
                page = (params or {}).get("page", 1)
                if page == 2:
                    return _make_response(200, page2)
                return _make_response(200, page1)
            if "repos/o/r/pulls/7" in url:
                return _make_response(200, detail_resp)
            if "repos/o/r/pulls" in url:
                return _make_response(200, list_resp)
            return _make_response(404, [], "")

    monkeypatch.setattr(real_httpx_module, "Client", MultiPageClient)

    forge = _forge(tmp_path)
    result = forge.list_review_comments(source_branch="feature/x")
    assert len(result) == 101
    assert result[0]["id"] == 0
    assert result[-1]["id"] == 200
    assert result[-1]["author"] == "last"


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


def test_pr_files_multi_page(tmp_path, monkeypatch):
    """More than 100 files → all pages are fetched (bug fix)."""
    list_resp = [{"number": 7}]
    detail_resp = {
        "number": 7,
        "merged": False,
        "state": "open",
        "html_url": "http://pr/7",
        "mergeable": True,
        "head": {"sha": "abc123"},
    }
    # 101 files across 2 pages
    page1 = [
        {
            "filename": f"src/file_{i}.py",
            "status": "modified",
            "additions": i,
            "deletions": 0,
        }
        for i in range(100)
    ]
    page2 = [
        {"filename": "src/last.py", "status": "added", "additions": 10, "deletions": 0}
    ]
    get_map = {
        "repos/o/r/pulls/7/files": _make_response(200, page1),
        "repos/o/r/pulls/7": _make_response(200, detail_resp),
        "repos/o/r/pulls": _make_response(200, list_resp),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    # Mock retrying_client to return page2 on second GET with page=2
    import httpx as real_httpx_module

    class MultiPageClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, headers=None, params=None, **kwargs):
            if "files" in url:
                page = (params or {}).get("page", 1)
                if page == 2:
                    return _make_response(200, page2)
                return _make_response(200, page1)
            if "repos/o/r/pulls/7" in url and "files" not in url:
                return _make_response(200, detail_resp)
            if "repos/o/r/pulls" in url and "files" not in url and "7" not in url:
                return _make_response(200, list_resp)
            return _make_response(404, [], "")

    monkeypatch.setattr(real_httpx_module, "Client", MultiPageClient)

    files = forge._pr_files(owner="o", repo="r", pull_number=7)
    assert len(files) == 101
    assert files[0]["path"] == "src/file_0.py"
    assert files[-1]["path"] == "src/last.py"


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
        repo_visibility_default="private",
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
        repo_visibility_default="private",
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


# ---------------------------------------------------------------------------
# update_repo
# ---------------------------------------------------------------------------


def test_update_repo_happy_path(tmp_path, monkeypatch):
    """200 from PATCH → returns True."""
    _mock_httpx(
        monkeypatch,
        patch_response=_make_response(200, {"description": "new desc"}),
    )
    forge = _forge(tmp_path, enable_repo_creation=True)
    result = forge.update_repo(owner="o", repo="r", description="new desc")
    assert result is True


def test_update_repo_non_200_returns_false(tmp_path, monkeypatch):
    """Non-200 from PATCH → returns False (never raises)."""
    _mock_httpx(
        monkeypatch,
        patch_response=_make_response(404, {}, "not found"),
    )
    forge = _forge(tmp_path, enable_repo_creation=True)
    result = forge.update_repo(owner="o", repo="r", description="desc")
    assert result is False


def test_update_repo_flag_disabled(tmp_path, monkeypatch):
    """enable_repo_creation=False (default) → NotConfiguredError."""
    forge = _forge(tmp_path)  # no enable_repo_creation
    with pytest.raises(NotConfiguredError, match="Repo metadata updates are disabled"):
        forge.update_repo(owner="o", repo="r", description="desc")


def test_update_repo_clamps_description(tmp_path, monkeypatch):
    """Description is clamped via _clamp_repo_description before PATCH."""
    captured = _mock_httpx(
        monkeypatch,
        patch_response=_make_response(200, {}),
    )
    forge = _forge(tmp_path, enable_repo_creation=True)
    long_desc = "x" * 400
    result = forge.update_repo(owner="o", repo="r", description=long_desc)
    assert result is True
    # Should be clamped to ≤350 chars with ellipsis
    sent = captured["patch_payload"]["description"]
    assert len(sent) <= 350
    assert sent.endswith("…")


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
    assert a["rule"] == "py/x"
    assert a["severity"] == "high"
    assert a["path"] == "tests/t.py"
    assert a["line"] == 92
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
    assert out[0]["rule"] == "py/x"
    assert out[0]["path"] == "src/a.py"


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
