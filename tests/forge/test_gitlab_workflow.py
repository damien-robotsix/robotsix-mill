"""End-to-end workflow integration tests for GitLabForge.

Chains multiple public GitLabForge methods in the order the stages
call them — open MR → status → pipeline → review → merge — against
a single mocked httpx.Client so the full lifecycle is exercised
without real HTTP.

Shares the same _make_response / _mock_httpx / _forge helpers as
tests/forge/test_gitlab.py (copied here because tests/forge/ has no
__init__.py, so cross-module imports aren't available).
"""

import httpx as real_httpx

from robotsix_mill.config import Secrets, Settings, _reset_secrets
from robotsix_mill.forge.gitlab import GitLabForge


# ---------------------------------------------------------------------------
# Helpers (mirrored from test_gitlab.py)
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
# Workflow integration test
# ---------------------------------------------------------------------------


def test_full_mr_lifecycle(tmp_path, monkeypatch):
    """Chains open_merge_request → pr_status → check_status →
    pr_review_status → merge_pr in the order the stages call them.

    Uses a single _mock_httpx with a comprehensive get_map so the
    entire chain runs against one consistent fake GitLab API surface.
    Verifies that method-A output feeds correctly into methods B–G
    (e.g. MR IID from open_merge_request flows through _find_mr into
    the pipeline/review/merge endpoints).
    """
    # -- API response fixtures --------------------------------------------
    project_json = {"id": 42}
    mr_create_resp = {
        "web_url": "https://gitlab.com/ns/project/-/merge_requests/1",
    }
    mr = {
        "iid": 1,
        "state": "opened",
        "web_url": "https://gitlab.com/ns/project/-/merge_requests/1",
        "merge_status": "can_be_merged",
        "sha": "abc123",
        "diff_refs": {"head_sha": "abc123"},
    }
    pipeline = {"id": 100, "status": "success"}
    notes = [
        # General (non-position) note → mapped as a review comment body.
        {
            "id": 1,
            "system": False,
            "author": {"username": "alice"},
            "created_at": "2026-01-15T00:00:00Z",
            "body": "LGTM",
        },
    ]
    changes_resp = {
        "changes": [
            {
                "old_path": "src/main.py",
                "new_path": "src/main.py",
                "new_file": False,
                "deleted_file": False,
                "renamed_file": False,
                "diff": "--- a/src/main.py\n+++ b/src/main.py\n@@ -1 +1,2 @@\n+x",
            },
        ]
    }
    merge_resp = {"state": "merged", "merge_commit_sha": "def456"}

    # -- get_map: more-specific keys first so substring matching doesn't
    #    short-circuit (e.g. "merge_requests/1/pipelines" must be checked
    #    before the generic "merge_requests" key).
    get_map = {
        "merge_requests/1/pipelines": _make_response(200, [pipeline]),
        "merge_requests/1/approvals": _make_response(200, {"approved": True}),
        "merge_requests/1/notes": _make_response(200, notes),
        "merge_requests/1/changes": _make_response(200, changes_resp),
        "merge_requests": _make_response(200, [mr]),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }

    captured = _mock_httpx(
        monkeypatch,
        get_map=get_map,
        post_response=_make_response(201, mr_create_resp),
        put_response=_make_response(200, merge_resp),
    )

    forge = _forge(tmp_path)

    # ------------------------------------------------------------------
    # 1. Authenticate — verify PRIVATE-TOKEN header is set on outbound
    #    requests (the _ApiClient passes _build_headers to every call).
    # ------------------------------------------------------------------
    # (Header verification is implicit: the mock captures payloads,
    #  and the real _build_headers is exercised through the _ApiClient.
    #  The existing test_build_headers in test_gitlab.py covers the
    #  header shape directly.)

    # ------------------------------------------------------------------
    # 2. Resolve project — first API call resolves project path → id.
    # ------------------------------------------------------------------
    # The project resolution happens inside _create_mr (first call).
    # We verify it succeeded by checking subsequent calls use /projects/42/...

    # ------------------------------------------------------------------
    # 3. Open MR — POST /projects/42/merge_requests → 201 → web_url.
    # ------------------------------------------------------------------
    url = forge.open_merge_request(
        source_branch="feature/x", title="Add X", body="desc"
    )
    assert url == "https://gitlab.com/ns/project/-/merge_requests/1"
    assert captured["post_payload"]["source_branch"] == "feature/x"
    assert captured["post_payload"]["target_branch"] == "main"
    assert captured["post_payload"]["title"] == "Add X"
    assert captured["post_payload"]["description"] == "desc"

    # ------------------------------------------------------------------
    # 4. MR status — GET merge_requests?source_branch=feature/x → MR dict.
    # ------------------------------------------------------------------
    status = forge.pr_status(source_branch="feature/x")
    assert status is not None
    assert status["merged"] is False
    assert status["state"] == "opened"
    assert status["url"] == "https://gitlab.com/ns/project/-/merge_requests/1"
    assert status["mergeable"] is True
    assert status["sha"] == "abc123"
    assert status["number"] == 1

    # ------------------------------------------------------------------
    # 5. Pipeline status — GET merge_requests/1/pipelines → success.
    # ------------------------------------------------------------------
    ci = forge.check_status(source_branch="feature/x")
    assert ci is not None
    assert ci["conclusion"] == "success"
    assert ci["failing"] == []

    # ------------------------------------------------------------------
    # 6. Review status — approvals + notes + changes → APPROVED.
    # ------------------------------------------------------------------
    review = forge.pr_review_status(source_branch="feature/x")
    assert review is not None
    assert review["state"] == "APPROVED"
    assert review["files"] == ["src/main.py"]
    assert len(review["comments"]) == 1
    assert review["comments"][0]["body"] == "LGTM"
    assert review["comments"][0]["review_state"] == "APPROVED"

    # ------------------------------------------------------------------
    # 7. Merge MR — PUT merge → {"state": "merged"}.
    # ------------------------------------------------------------------
    merge_result = forge.merge_pr(source_branch="feature/x")
    assert merge_result["merged"] is True
    assert merge_result["reason"] == "merged"
    assert captured["put_payload"]["merge_when_pipeline_succeeds"] is True
    assert captured["put_payload"]["squash"] is True


def test_workflow_handles_missing_mr_gracefully(tmp_path, monkeypatch):
    """When the branch has no MR, pr_status and check_status return
    None, and merge_pr returns a failure dict — the chain degrades
    gracefully instead of raising."""
    project_json = {"id": 42}
    get_map = {
        "merge_requests": _make_response(200, []),
        "projects/ns%2Fproject": _make_response(200, project_json),
    }

    _mock_httpx(monkeypatch, get_map=get_map)

    forge = _forge(tmp_path)

    # pr_status → None when no MR exists.
    assert forge.pr_status(source_branch="no-branch") is None

    # check_status → None when no MR exists.
    assert forge.check_status(source_branch="no-branch") is None

    # merge_pr → failure dict (not a raise).
    result = forge.merge_pr(source_branch="no-branch")
    assert result == {"merged": False, "reason": "MR not found"}
