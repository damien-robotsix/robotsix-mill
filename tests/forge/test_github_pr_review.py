"""Test the GitHubForge PR-lifecycle / review-status HTTP seams with a mocked
httpx.Client: _delete_branch, cross-repo create-pr retry, 401 self-heal,
CI-conclusion derivation, pr_review_status, and the pure helpers that
_parse_pr_detail / _statuses_to_check_runs / _latest_definitive_runs /
_extract_annotations expose.
"""

from datetime import UTC

import httpx as real_httpx
import pytest

from robotsix_mill.config import Secrets, Settings, _reset_secrets
from robotsix_mill.forge.github import GitHubForge
from robotsix_mill.forge.github_ci import (
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
    assert forge_no_rc._head_owner == "o"  # from default forge_remote_url


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
    # validator (forge_auth=app requires github_app_id / private_key)
    # can see them.
    _reset_secrets()
    _cfg._secrets = Secrets(
        github_app_id=kw.get("GITHUB_APP_ID", "123"),
        github_app_private_key=kw.get("GITHUB_APP_PRIVATE_KEY", "KEY"),
    )
    kw.setdefault("data_dir", str(tmp_path))
    kw.setdefault("forge_kind", "github")
    kw.setdefault("forge_auth", "app")
    kw.setdefault("forge_remote_url", "https://github.com/o/r.git")
    # github_app_id and github_app_private_key are now Secrets-only fields;
    # pop before Settings()
    kw.pop("GITHUB_APP_ID", None)
    kw.pop("GITHUB_APP_PRIVATE_KEY", None)
    return Settings(**kw)


def test_create_pr_401_retry_then_201_success(tmp_path, monkeypatch):
    """First POST returns 401, retry returns 201 — PR opens successfully.
    The library's github_token is called exactly twice (initial + retry)."""
    import time as _time

    import robotsix_github_auth as rga

    rga.clear_token_cache()
    mint_calls = []

    def fake_github_token(**kwargs):
        mint_calls.append(_time.time())
        return f"ghs_{len(mint_calls)}"

    monkeypatch.setattr(rga, "github_token", fake_github_token)
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
    The library's github_token is called exactly twice (initial + retry)."""
    import time as _time

    import robotsix_github_auth as rga

    rga.clear_token_cache()
    mint_calls = []

    def fake_github_token(**kwargs):
        mint_calls.append(_time.time())
        return f"ghs_{len(mint_calls)}"

    monkeypatch.setattr(rga, "github_token", fake_github_token)
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
    assert list(out["failing"])
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


def test_commit_ci_conclusion_no_checkruns_but_run_in_flight_is_pending(
    tmp_path, monkeypatch
):
    """Zero check-runs + an in-flight workflow run → pending, NOT success.

    A just-pushed SHA has a window where its workflow run exists but no
    check-run is registered yet. Reading that window as "no CI configured"
    let the merge stage merge red PRs (hexarchy #286/#287, 2026-09-05).
    """
    runs_data = {
        "workflow_runs": [
            {
                "id": 900,
                "name": "CI",
                "workflow_id": 77,
                "head_sha": "abc123",
                "conclusion": None,
                "status": "in_progress",
                "html_url": "https://github.com/o/r/actions/runs/900",
                "created_at": "2026-09-05T12:42:11Z",
                "event": "pull_request",
                "head_branch": "mill/some-ticket",
                "path": ".github/workflows/ci.yml",
            }
        ]
    }
    get_map = {
        "commits/abc123/check-runs": _make_response(200, {"check_runs": []}),
        "commits/abc123/status": _make_response(200, {"statuses": []}),
        "actions/runs": _make_response(200, runs_data),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.commit_ci_conclusion(sha="abc123")
    assert result is not None
    assert result["conclusion"] == "pending"
    assert result["pending"] == ["CI"]
    assert result.get("_no_checks") is True


def test_commit_ci_conclusion_no_checkruns_startup_failure_run(tmp_path, monkeypatch):
    """Zero check-runs + a completed startup_failure run → failure.

    A workflow that fails to parse registers no check-run; the run-level
    cross-check must fail the gate off the same any-status listing.
    """
    runs_data = {
        "workflow_runs": [
            {
                "id": 901,
                "name": "CI",
                "workflow_id": 77,
                "head_sha": "abc123",
                "conclusion": "startup_failure",
                "status": "completed",
                "html_url": "https://github.com/o/r/actions/runs/901",
                "created_at": "2026-09-05T12:42:11Z",
                "event": "pull_request",
                "head_branch": "mill/some-ticket",
                "path": ".github/workflows/ci.yml",
            }
        ]
    }
    get_map = {
        "commits/abc123/check-runs": _make_response(200, {"check_runs": []}),
        "commits/abc123/status": _make_response(200, {"statuses": []}),
        "actions/runs": _make_response(200, runs_data),
    }
    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)
    result = forge.commit_ci_conclusion(sha="abc123")
    assert result is not None
    assert result["conclusion"] == "failure"
    assert result["failing"] and result["failing"][0]["name"] == "CI"


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

    forge = _forge(tmp_path)
    result = forge.commit_ci_conclusion(sha="abc123")
    # Should succeed after retry.
    assert result is not None
    assert result["conclusion"] == "success"
    # The retry loop should have been entered (call_count tracks
    # httpx.Client.get() calls across both attempts).
    assert call_count[0] >= 2


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
        {
            "id": 1,
            "state": "CHANGES_REQUESTED",
            "body": "Please fix X",
            "commit_id": "abc111",
        },
        {"id": 2, "state": "APPROVED", "body": "LGTM!", "commit_id": "abc222"},
        {"id": 3, "state": "DISMISSED", "body": "dismissed", "commit_id": "abc333"},
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
    assert result["commit_id"] == "abc222"
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
    # Retry happened: the first get() returned 401, the second succeeded.
    assert call_count[0] >= 2


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
    # Retry happened: the first comments GET returned 401, the second succeeded.
    assert reviews_401_done[0]


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
        {"id": 1, "state": "DISMISSED", "body": "stale", "commit_id": "abc111"},
        {"id": 2, "state": "DISMISSED", "body": "also stale", "commit_id": "abc222"},
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
    assert result["commit_id"] == "abc222"  # latest dismissed review
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


def test__pr_review_status_multi_page(tmp_path, monkeypatch):
    """More than 100 reviews/comments → all pages are fetched (bug fix)."""
    reviews_page1 = [
        {"id": i, "state": "APPROVED", "body": f"LGTM {i}"} for i in range(100)
    ]
    reviews_page2 = [{"id": 200, "state": "APPROVED", "body": "final approval"}]
    comments_page1 = [
        {
            "id": i,
            "user": {"login": f"user{i}"},
            "created_at": "2025-01-15T12:00:00Z",
            "body": f"nit {i}",
            "path": f"src/file_{i}.py",
            "line": i,
            "diff_hunk": "@@ ... @@",
            "pull_request_review_id": 0,
        }
        for i in range(100)
    ]
    comments_page2 = [
        {
            "id": 300,
            "user": {"login": "last"},
            "created_at": "2025-01-16T12:00:00Z",
            "body": "last comment",
            "path": "src/last.py",
            "line": 99,
            "diff_hunk": "@@ ... @@",
            "pull_request_review_id": 200,
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
            page = (params or {}).get("page", 1)
            if "reviews" in url:
                if page == 2:
                    return _make_response(200, reviews_page2)
                return _make_response(200, reviews_page1)
            if "comments" in url:
                if page == 2:
                    return _make_response(200, comments_page2)
                return _make_response(200, comments_page1)
            if "files" in url:
                return _make_response(200, [])
            return _make_response(404, [], "")

    monkeypatch.setattr(real_httpx_module, "Client", MultiPageClient)

    forge = _forge(tmp_path)
    result = forge._pr_review_status(owner="o", repo="r", pull_number=7)

    assert result["state"] == "APPROVED"
    assert result["files"] == []
    # 101 review body comments + 101 inline comments = 202 total
    assert len(result["comments"]) == 202
    review_bodies = [c for c in result["comments"] if c["path"] == ""]
    assert len(review_bodies) == 101
    assert review_bodies[-1]["body"] == "final approval"


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
    from datetime import datetime

    for val in (None, ""):
        result = _parse_iso_utc(val)
        assert result == datetime.fromtimestamp(0, tz=UTC)


def test_parse_iso_utc_invalid():
    """Unparseable string → Unix epoch (UTC)."""
    from datetime import datetime

    result = _parse_iso_utc("not-a-date")
    assert result == datetime.fromtimestamp(0, tz=UTC)


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
        "author": "",
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


def test_statuses_to_check_runs_uses_per_context_state_not_combined():
    """Each status context keeps ITS OWN state — the combined-status
    ``state`` must NOT smear onto every context.

    Regression for 2026-09-03 (robotsix-chat#1807): the combined
    ``state`` is the aggregate across all contexts (``"failure"`` when
    ANY one failed), so using it for every context misassembled the
    cross-repo ci-fix failing list — a green context such as "All CI
    checks passed" was reported as a failing check, and a PR whose only
    real failure was elsewhere was gated on checks that had passed.
    """
    data = {
        "state": "failure",
        "statuses": [
            {"context": "All CI checks passed", "state": "success"},
            {"context": "Pre-commit hooks", "state": "failure"},
        ],
    }
    runs = _statuses_to_check_runs(data)
    by_name = {r["name"]: r for r in runs}
    assert by_name["All CI checks passed"]["conclusion"] == "success"
    assert by_name["All CI checks passed"]["status"] == "completed"
    assert by_name["Pre-commit hooks"]["conclusion"] == "failure"
    assert by_name["Pre-commit hooks"]["status"] == "completed"
    # Fallback: a status entry without its own state still uses the
    # combined state, so the legacy single-status shape keeps working.
    legacy = _statuses_to_check_runs(
        {"state": "pending", "statuses": [{"context": "ci/test"}]}
    )
    assert legacy[0]["conclusion"] is None
    assert legacy[0]["status"] == "in_progress"


def test_statuses_to_check_runs_error_state_gates_merge():
    """A commit-status context whose own state is ``"error"`` must produce a
    FAILING conclusion and gate the merge.

    Commit-status state is one of error/failure/pending/success, but the
    Checks-API conclusion vocabulary has no ``"error"`` — so without
    normalization ``_conclusion_for_check`` classifies an errored context as
    ``"neutral"`` and it stops gating the merge (regression vs the old
    combined roll-up, which GitHub reports as ``"failure"`` whenever any
    context errored).  Normalize ``"error"`` -> ``"failure"``.
    """
    from robotsix_mill.forge.github_ci import _conclusion_for_check

    data = {
        "state": "error",
        "statuses": [{"context": "deploy/preview", "state": "error"}],
    }
    runs = _statuses_to_check_runs(data)
    assert len(runs) == 1
    assert runs[0]["name"] == "deploy/preview"
    assert runs[0]["status"] == "completed"
    assert runs[0]["conclusion"] == "failure"
    # And it is classified as a genuine failure that gates the merge.
    assert _conclusion_for_check(runs[0]) == "failure"


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
    assert result["conclusion"] is None  # cr dict has no "conclusion" key
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
        "conclusion": None,
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
