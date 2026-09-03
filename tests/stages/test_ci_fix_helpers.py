"""Unit tests for stateless helpers in ci_fix_helpers.py.

Covers functions not already tested in test_ci_fix.py or test_codeql_fp_triage.py:
_format_code_scanning_alerts, _format_labelled_alerts, _format_alert_refs,
_alert_loc, _write_text, and _FailingContext.
"""

import pytest

from robotsix_mill.stages.ci_fix_helpers import (
    _alert_loc,
    _FailingContext,
    _format_alert_refs,
    _format_code_scanning_alerts,
    _format_labelled_alerts,
    _normalize_ci_failure_reason,
    _write_text,
)

# ---------------------------------------------------------------------------
# _alert_loc
# ---------------------------------------------------------------------------


def test_alert_loc_with_line():
    assert _alert_loc({"path": "src/foo.py", "line": 42}) == "src/foo.py:42"


def test_alert_loc_without_line():
    assert _alert_loc({"path": "src/foo.py"}) == "src/foo.py"


def test_alert_loc_empty_path():
    assert _alert_loc({}) == ""


def test_alert_loc_line_but_no_path():
    assert _alert_loc({"line": 10}) == ":10"


# ---------------------------------------------------------------------------
# _format_alert_refs
# ---------------------------------------------------------------------------


def test_format_alert_refs_empty():
    assert _format_alert_refs([]) == ""


def test_format_alert_refs_single():
    refs = _format_alert_refs(
        [{"rule": "py/unused-import", "path": "src/foo.py", "line": 10}]
    )
    assert refs == "py/unused-import @ src/foo.py:10"


def test_format_alert_refs_multiple():
    refs = _format_alert_refs(
        [
            {"rule": "py/unused-import", "path": "src/foo.py", "line": 10},
            {"rule": "py/empty-except", "path": "src/bar.py", "line": 20},
        ]
    )
    assert "py/unused-import @ src/foo.py:10" in refs
    assert "py/empty-except @ src/bar.py:20" in refs
    assert ";" in refs  # semicolon separator


def test_format_alert_refs_missing_fields():
    refs = _format_alert_refs(
        [{"path": "src/foo.py", "line": 5}]  # no rule
    )
    assert refs == " @ src/foo.py:5"


# ---------------------------------------------------------------------------
# _format_code_scanning_alerts
# ---------------------------------------------------------------------------


def test_format_code_scanning_alerts_empty():
    assert _format_code_scanning_alerts([]) == ""


def test_format_code_scanning_alerts_single():
    result = _format_code_scanning_alerts(
        [
            {
                "rule": "py/unused-import",
                "severity": "high",
                "path": "src/foo.py",
                "line": 10,
                "message": "Unused import os",
            }
        ]
    )
    assert "Code-scanning alerts" in result
    assert "[high] `py/unused-import` src/foo.py:10: Unused import os" in result


def test_format_code_scanning_alerts_multiple():
    result = _format_code_scanning_alerts(
        [
            {
                "rule": "py/unused-import",
                "severity": "high",
                "path": "src/foo.py",
                "line": 10,
                "message": "Unused import os",
            },
            {
                "rule": "py/empty-except",
                "severity": "low",
                "path": "src/bar.py",
                "line": 20,
                "message": "Empty except block",
            },
        ]
    )
    assert "Code-scanning alerts" in result
    assert "[high] `py/unused-import` src/foo.py:10: Unused import os" in result
    assert "[low] `py/empty-except` src/bar.py:20: Empty except block" in result


def test_format_code_scanning_alerts_missing_severity():
    result = _format_code_scanning_alerts(
        [
            {
                "rule": "py/x",
                "path": "src/foo.py",
                "line": 1,
                "message": "bad",
            }
        ]
    )
    assert "[?] `py/x`" in result


def test_format_code_scanning_alerts_no_line():
    result = _format_code_scanning_alerts(
        [
            {
                "rule": "py/x",
                "severity": "warning",
                "path": "src/foo.py",
                "message": "bad",
            }
        ]
    )
    # No line number appended — the colon comes from the message separator.
    assert "src/foo.py: bad" in result
    assert "bad" in result


# ---------------------------------------------------------------------------
# _format_labelled_alerts
# ---------------------------------------------------------------------------


def test_format_labelled_alerts_empty():
    assert _format_labelled_alerts([], []) == ""


def test_format_labelled_alerts_in_scope_only():
    in_scope = [
        {
            "rule": "py/unused-import",
            "severity": "high",
            "path": "src/foo.py",
            "line": 10,
            "message": "Unused import os",
        }
    ]
    result = _format_labelled_alerts(in_scope, [])
    assert "Code-scanning alerts" in result
    assert "THIS PR's own changed files" in result
    assert "MUST be fixed in-scope" in result
    assert "IN THIS PR'S DIFF — must fix" in result
    assert "[high] `py/unused-import` src/foo.py:10: Unused import os" in result
    assert "untouched file" not in result.lower()
    assert "out-of-scope" not in result


def test_format_labelled_alerts_out_of_scope_only():
    out_of_scope = [
        {
            "rule": "py/empty-except",
            "severity": "low",
            "path": "src/untouched.py",
            "line": 20,
            "message": "Empty except",
        }
    ]
    result = _format_labelled_alerts([], out_of_scope)
    assert "Code-scanning alerts" in result
    assert "untouched files" in result
    assert "out-of-scope candidate" in result
    assert "[low] `py/empty-except` src/untouched.py:20: Empty except" in result
    assert "must fix" not in result.lower()


def test_format_labelled_alerts_both():
    in_scope = [
        {
            "rule": "py/unused-import",
            "severity": "high",
            "path": "src/foo.py",
            "line": 10,
            "message": "Unused import os",
        }
    ]
    out_of_scope = [
        {
            "rule": "py/empty-except",
            "severity": "low",
            "path": "src/bar.py",
            "line": 20,
            "message": "Empty except",
        }
    ]
    result = _format_labelled_alerts(in_scope, out_of_scope)
    assert "THIS PR's own changed files" in result
    assert "IN THIS PR'S DIFF — must fix" in result
    assert "untouched files" in result
    assert "out-of-scope candidate" in result
    assert "py/unused-import" in result
    assert "py/empty-except" in result


def test_format_labelled_alerts_in_scope_missing_severity():
    in_scope = [
        {
            "rule": "py/x",
            "path": "src/foo.py",
            "line": 1,
            "message": "bad",
        }
    ]
    result = _format_labelled_alerts(in_scope, [])
    assert "[?] `py/x`" in result


# ---------------------------------------------------------------------------
# _write_text
# ---------------------------------------------------------------------------


def test_write_text_basic(tmp_path):
    p = tmp_path / "test.txt"
    _write_text(p, "hello")
    assert p.read_text(encoding="utf-8") == "hello"


def test_write_text_creates_parent_dirs(tmp_path):
    p = tmp_path / "deeply" / "nested" / "dir" / "test.txt"
    _write_text(p, "nested content")
    assert p.read_text(encoding="utf-8") == "nested content"


def test_write_text_overwrite(tmp_path):
    p = tmp_path / "test.txt"
    _write_text(p, "first")
    _write_text(p, "second")
    assert p.read_text(encoding="utf-8") == "second"


def test_write_text_multiline(tmp_path):
    p = tmp_path / "test.txt"
    _write_text(p, "line 1\nline 2\n")
    assert p.read_text(encoding="utf-8") == "line 1\nline 2\n"


# ---------------------------------------------------------------------------
# _FailingContext
# ---------------------------------------------------------------------------


def test_failing_context_defaults():
    ctx = _FailingContext(
        repo_dir="/repo",
        branch="mill/test",
        failing_summary="CI failed",
    )
    assert ctx.repo_dir == "/repo"
    assert ctx.branch == "mill/test"
    assert ctx.failing_summary == "CI failed"
    assert ctx.failing == []
    assert ctx.alerts == []
    assert ctx.changed_paths == set()
    assert ctx.alerts_unreadable is False


def test_failing_context_full():
    ctx = _FailingContext(
        repo_dir="/repo",
        branch="mill/test",
        failing_summary="CI failed",
        failing=[{"name": "lint"}],
        alerts=[{"rule": "py/x"}],
        changed_paths={"src/foo.py"},
        alerts_unreadable=True,
    )
    assert ctx.repo_dir == "/repo"
    assert ctx.branch == "mill/test"
    assert ctx.failing_summary == "CI failed"
    assert ctx.failing == [{"name": "lint"}]
    assert ctx.alerts == [{"rule": "py/x"}]
    assert ctx.changed_paths == {"src/foo.py"}
    assert ctx.alerts_unreadable is True


def test_failing_context_is_namedtuple():
    ctx = _FailingContext(
        repo_dir="/repo",
        branch="mill/test",
        failing_summary="CI failed",
    )
    assert hasattr(ctx, "_fields")
    assert "repo_dir" in ctx._fields
    assert "branch" in ctx._fields
    assert "failing_summary" in ctx._fields
    assert "failing" in ctx._fields
    assert "alerts" in ctx._fields
    assert "changed_paths" in ctx._fields
    assert "alerts_unreadable" in ctx._fields
    assert "head_sha" in ctx._fields
    assert "failing_run_ids" in ctx._fields
    assert "failing_run_urls" in ctx._fields


# ---------------------------------------------------------------------------
# _check_upstream_ci_breakage
# ---------------------------------------------------------------------------


class TestCheckUpstreamCiBreakage:
    """Tests for _check_upstream_ci_breakage."""

    def test_target_branch_green_returns_none(self, monkeypatch):
        """When the target branch CI is green, the check returns None."""
        from robotsix_mill.config import Settings
        from robotsix_mill.stages.ci_fix_helpers import _check_upstream_ci_breakage

        monkeypatch.setattr(
            "robotsix_mill.config.repos.target_branch_for",
            lambda s, rc: "main",
        )
        monkeypatch.setattr(
            "robotsix_mill.vcs.git_ops.remote_branch_sha",
            lambda repo, branch: "abc123def456",
        )
        mock_forge = _FakeForge(
            commit_ci_conclusion={
                "conclusion": "success",
                "failing": [],
            }
        )
        monkeypatch.setattr(
            "robotsix_mill.forge.get_forge",
            lambda s, repo_config: mock_forge,
        )

        result = _check_upstream_ci_breakage(
            "test-ticket",
            Settings(),
            None,
            "/fake/repo",
            [{"name": "ruff", "conclusion": "failure"}],
        )
        assert result is None

    def test_target_branch_failing_different_checks_returns_none(self, monkeypatch):
        """When the target branch fails with DIFFERENT checks, return None."""
        from robotsix_mill.config import Settings
        from robotsix_mill.stages.ci_fix_helpers import _check_upstream_ci_breakage

        monkeypatch.setattr(
            "robotsix_mill.config.repos.target_branch_for",
            lambda s, rc: "main",
        )
        monkeypatch.setattr(
            "robotsix_mill.vcs.git_ops.remote_branch_sha",
            lambda repo, branch: "abc123def456",
        )
        mock_forge = _FakeForge(
            commit_ci_conclusion={
                "conclusion": "failure",
                "failing": [{"name": "Security Audit"}],
            }
        )
        monkeypatch.setattr(
            "robotsix_mill.forge.get_forge",
            lambda s, repo_config: mock_forge,
        )

        result = _check_upstream_ci_breakage(
            "test-ticket",
            Settings(),
            None,
            "/fake/repo",
            [{"name": "ruff"}],
        )
        assert result is None

    def test_ci_source_ticket_is_exempt(self, monkeypatch):
        """A source=ci ticket exists to fix the target branch — never park it
        behind the breakage it was filed for (robotsix-chat 4c2b, 2026-08-26)."""
        from robotsix_mill.config import Settings
        from robotsix_mill.stages.ci_fix_helpers import _check_upstream_ci_breakage

        monkeypatch.setattr(
            "robotsix_mill.config.repos.target_branch_for",
            lambda s, rc: "main",
        )
        calls: list[str] = []

        def _sha(repo, branch):
            calls.append("sha")
            return "abc123def4567890"

        monkeypatch.setattr("robotsix_mill.vcs.git_ops.remote_branch_sha", _sha)
        mock_forge = _FakeForge(
            commit_ci_conclusion={
                "conclusion": "failure",
                "failing": [{"name": "Container image scan (Trivy)"}],
            }
        )
        monkeypatch.setattr(
            "robotsix_mill.forge.get_forge",
            lambda s, repo_config: mock_forge,
        )
        failing = [{"name": "Container image scan (Trivy)"}]

        assert (
            _check_upstream_ci_breakage(
                "t", Settings(), None, "/fake/repo", failing, ticket_source="ci"
            )
            is None
        )
        assert calls == [], "exemption short-circuits before any lookup"
        # Same inputs, any other source → still parked.
        assert (
            _check_upstream_ci_breakage(
                "t", Settings(), None, "/fake/repo", failing, ticket_source="user"
            )
            is not None
        )

    def test_block_note_carries_marker_and_auto_resume_hint(self, monkeypatch):
        from robotsix_mill.config import Settings
        from robotsix_mill.stages.ci_fix_helpers import (
            UPSTREAM_CI_BLOCK_MARKER,
            _check_upstream_ci_breakage,
        )

        monkeypatch.setattr(
            "robotsix_mill.config.repos.target_branch_for", lambda s, rc: "main"
        )
        monkeypatch.setattr(
            "robotsix_mill.vcs.git_ops.remote_branch_sha",
            lambda r, b: "abc123def4567890",
        )
        monkeypatch.setattr(
            "robotsix_mill.forge.get_forge",
            lambda s, repo_config: _FakeForge(
                commit_ci_conclusion={
                    "conclusion": "failure",
                    "failing": [{"name": "ruff"}],
                }
            ),
        )
        note = _check_upstream_ci_breakage(
            "t", Settings(), None, "/fake/repo", [{"name": "ruff"}]
        )
        assert note is not None and note.startswith(UPSTREAM_CI_BLOCK_MARKER)
        assert "resumes automatically" in note

    def test_same_check_failing_on_both_returns_block_message(self, monkeypatch):
        """When the same check fails on both PR and target, return block message."""
        from robotsix_mill.config import Settings
        from robotsix_mill.stages.ci_fix_helpers import _check_upstream_ci_breakage

        monkeypatch.setattr(
            "robotsix_mill.config.repos.target_branch_for",
            lambda s, rc: "main",
        )
        monkeypatch.setattr(
            "robotsix_mill.vcs.git_ops.remote_branch_sha",
            lambda repo, branch: "abc123def4567890",
        )
        mock_forge = _FakeForge(
            commit_ci_conclusion={
                "conclusion": "failure",
                "failing": [
                    {"name": "ruff"},
                    {"name": "CI"},
                ],
            }
        )
        monkeypatch.setattr(
            "robotsix_mill.forge.get_forge",
            lambda s, repo_config: mock_forge,
        )

        result = _check_upstream_ci_breakage(
            "test-ticket",
            Settings(),
            None,
            "/fake/repo",
            [
                {"name": "ruff"},
                {"name": "mypy"},
            ],
        )
        assert result is not None
        assert "Upstream CI breakage" in result
        assert "ruff" in result
        assert "main" in result
        assert "abc123de" in result

    def test_target_sha_unresolvable_returns_none(self, monkeypatch):
        """When the target branch SHA can't be resolved, return None."""
        from robotsix_mill.config import Settings
        from robotsix_mill.stages.ci_fix_helpers import _check_upstream_ci_breakage

        monkeypatch.setattr(
            "robotsix_mill.config.repos.target_branch_for",
            lambda s, rc: "main",
        )
        monkeypatch.setattr(
            "robotsix_mill.vcs.git_ops.remote_branch_sha",
            lambda repo, branch: None,
        )
        # Forge lists no runs → falls back to the (None) local ref.
        monkeypatch.setattr(
            "robotsix_mill.forge.get_forge",
            lambda s, repo_config: _FakeForge(workflow_runs=[]),
        )

        result = _check_upstream_ci_breakage(
            "test-ticket",
            Settings(),
            None,
            "/fake/repo",
            [{"name": "ruff"}],
        )
        assert result is None

    def test_commit_ci_conclusion_raises_returns_none(self, monkeypatch):
        """When commit_ci_conclusion raises, return None (fall through)."""
        from robotsix_mill.config import Settings
        from robotsix_mill.stages.ci_fix_helpers import _check_upstream_ci_breakage

        monkeypatch.setattr(
            "robotsix_mill.config.repos.target_branch_for",
            lambda s, rc: "main",
        )
        monkeypatch.setattr(
            "robotsix_mill.vcs.git_ops.remote_branch_sha",
            lambda repo, branch: "abc123",
        )

        def _raise(*a, **k):
            raise RuntimeError("forge unavailable")

        monkeypatch.setattr(
            "robotsix_mill.forge.get_forge",
            lambda s, repo_config: _FakeForge(
                commit_ci_conclusion=_raise,
            ),
        )

        result = _check_upstream_ci_breakage(
            "test-ticket",
            Settings(),
            None,
            "/fake/repo",
            [{"name": "ruff"}],
        )
        assert result is None

    def test_resolves_current_forge_head_not_stale_local_ref(self, monkeypatch):
        """The guard evaluates the target branch's CURRENT head from the
        forge, not the (possibly stale) local remote-tracking ref captured
        when the ticket was parked.

        Regression for robotsix-board main advancing 7fc8e9d4 -> 53000db1
        (2026-09-03): the old sha's CI was red for CodeQL, the new tip is
        green, yet the guard kept re-blocking on the stale sha.  Once the
        current tip is green for the cited checks it must NOT re-block.
        """
        from robotsix_mill.config import Settings
        from robotsix_mill.stages.ci_fix_helpers import _check_upstream_ci_breakage

        monkeypatch.setattr(
            "robotsix_mill.config.repos.target_branch_for",
            lambda s, rc: "main",
        )
        # Local clone is pinned to the STALE sha whose CI was red.
        monkeypatch.setattr(
            "robotsix_mill.vcs.git_ops.remote_branch_sha",
            lambda repo, branch: "7fc8e9d4stale",
        )
        seen: list[str] = []

        def _ccc(*, sha):
            seen.append(sha)
            if sha == "7fc8e9d4stale":
                return {"conclusion": "failure", "failing": [{"name": "CodeQL"}]}
            # Current tip is green.
            return {"conclusion": "success", "failing": []}

        mock_forge = _FakeForge(
            commit_ci_conclusion=_ccc,
            workflow_runs=[{"head_sha": "53000db1fresh"}],
        )
        monkeypatch.setattr(
            "robotsix_mill.forge.get_forge",
            lambda s, repo_config: mock_forge,
        )

        result = _check_upstream_ci_breakage(
            "test-ticket",
            Settings(),
            None,
            "/fake/repo",
            [{"name": "CodeQL"}],
        )
        # Evaluated against the fresh forge head, which is green — no re-block.
        assert result is None
        assert seen == ["53000db1fresh"], (
            "guard must query the current forge head, not the stale local ref"
        )

    def test_falls_back_to_local_ref_when_forge_has_no_runs(self, monkeypatch):
        """When the forge lists no runs for the target, the guard falls back
        to the local remote-tracking ref so behaviour degrades gracefully."""
        from robotsix_mill.config import Settings
        from robotsix_mill.stages.ci_fix_helpers import _check_upstream_ci_breakage

        monkeypatch.setattr(
            "robotsix_mill.config.repos.target_branch_for",
            lambda s, rc: "main",
        )
        monkeypatch.setattr(
            "robotsix_mill.vcs.git_ops.remote_branch_sha",
            lambda repo, branch: "abc123def4567890",
        )
        seen: list[str] = []

        def _ccc(*, sha):
            seen.append(sha)
            return {"conclusion": "failure", "failing": [{"name": "ruff"}]}

        mock_forge = _FakeForge(commit_ci_conclusion=_ccc, workflow_runs=[])
        monkeypatch.setattr(
            "robotsix_mill.forge.get_forge",
            lambda s, repo_config: mock_forge,
        )

        result = _check_upstream_ci_breakage(
            "test-ticket",
            Settings(),
            None,
            "/fake/repo",
            [{"name": "ruff"}],
        )
        assert result is not None
        assert seen == ["abc123def4567890"]

    def test_target_ci_pending_returns_none(self, monkeypatch):
        """When the target branch CI is pending, return None."""
        from robotsix_mill.config import Settings
        from robotsix_mill.stages.ci_fix_helpers import _check_upstream_ci_breakage

        monkeypatch.setattr(
            "robotsix_mill.config.repos.target_branch_for",
            lambda s, rc: "main",
        )
        monkeypatch.setattr(
            "robotsix_mill.vcs.git_ops.remote_branch_sha",
            lambda repo, branch: "abc123",
        )
        mock_forge = _FakeForge(
            commit_ci_conclusion={
                "conclusion": "pending",
                "failing": [{"name": "ruff"}],
            }
        )
        monkeypatch.setattr(
            "robotsix_mill.forge.get_forge",
            lambda s, repo_config: mock_forge,
        )

        result = _check_upstream_ci_breakage(
            "test-ticket",
            Settings(),
            None,
            "/fake/repo",
            [{"name": "ruff"}],
        )
        assert result is None

    def test_multiple_common_failing_checks(self, monkeypatch):
        """When multiple checks fail on both, all are named in the message."""
        from robotsix_mill.config import Settings
        from robotsix_mill.stages.ci_fix_helpers import _check_upstream_ci_breakage

        monkeypatch.setattr(
            "robotsix_mill.config.repos.target_branch_for",
            lambda s, rc: "develop",
        )
        monkeypatch.setattr(
            "robotsix_mill.vcs.git_ops.remote_branch_sha",
            lambda repo, branch: "deadbeefcafe",
        )
        mock_forge = _FakeForge(
            commit_ci_conclusion={
                "conclusion": "failure",
                "failing": [
                    {"name": "ruff"},
                    {"name": "mypy"},
                    {"name": "Security Audit"},
                ],
            }
        )
        monkeypatch.setattr(
            "robotsix_mill.forge.get_forge",
            lambda s, repo_config: mock_forge,
        )

        result = _check_upstream_ci_breakage(
            "test-ticket",
            Settings(),
            None,
            "/fake/repo",
            [
                {"name": "mypy"},
                {"name": "ruff"},
            ],
        )
        assert result is not None
        assert "Upstream CI breakage" in result
        assert "mypy" in result
        assert "ruff" in result
        assert "Security Audit" not in result  # not failing on PR
        assert "develop" in result
        assert "deadbeef" in result


# --- fake forge for _check_upstream_ci_breakage tests ---


class _FakeForge:
    """Minimal forge stub that returns a canned dict (or raises) from
    ``commit_ci_conclusion`` and a canned list of runs from
    ``list_workflow_runs``.

    ``workflow_runs`` defaults to an empty list so the guard falls back to
    the (monkeypatched) ``remote_branch_sha`` local ref — preserving the
    pre-existing tests that only care about ``commit_ci_conclusion``."""

    def __init__(self, commit_ci_conclusion=None, workflow_runs=None):
        self._ccc = commit_ci_conclusion
        self._runs = workflow_runs if workflow_runs is not None else []

    def commit_ci_conclusion(self, *, sha):
        if callable(self._ccc):
            return self._ccc(sha=sha)
        return self._ccc

    def list_workflow_runs(self, *, branch):
        if callable(self._runs):
            return self._runs(branch=branch)
        return self._runs


# ---------------------------------------------------------------------------
# _normalize_ci_failure_reason
# ---------------------------------------------------------------------------


class TestNormalizeCiFailureReason:
    """Ensure the normalized key clusters genuine recurrences but
    separates distinct root causes (the 'c0dff799' 31-ticket bucket
    problem)."""

    def _ann(self, message: str, path: str = "src/foo.py", line: int = 10) -> dict:
        return {"level": "error", "path": path, "start_line": line, "message": message}

    def _failing(self, annotations: list | None = None) -> list[dict]:
        chk: dict = {"name": "ci / Tests"}
        if annotations is not None:
            chk["annotations"] = annotations
        return [chk]

    @staticmethod
    def _summary(failing: list[dict]) -> str:
        """Build a realistic failing-summary matching CI output format."""
        from robotsix_mill.stages.ci_fix_helpers import _build_failing_summary

        return _build_failing_summary(failing)

    def test_distinct_annotation_messages_yield_distinct_keys(self) -> None:
        f1 = self._failing([self._ann("Incompatible types in expression")])
        f2 = self._failing([self._ann("unused variable F841")])
        k1 = _normalize_ci_failure_reason(f1, self._summary(f1))
        k2 = _normalize_ci_failure_reason(f2, self._summary(f2))
        assert k1 != k2

    @pytest.mark.parametrize(
        ("msg_a", "msg_b"),
        [
            ("Incompatible types in expression", "Missing return statement"),
            ("Name 'x' is not defined", "Cannot assign to 'None'"),
            ("AssertionError: assert 1 == 2", "ruff rule F841"),
        ],
        ids=["mypy-types-vs-return", "name-vs-assign", "assert-vs-ruff"],
    )
    def test_various_root_causes_produce_distinct_keys(
        self, msg_a: str, msg_b: str
    ) -> None:
        """Different annotation message root causes must not collide."""
        f1 = self._failing([self._ann(msg_a)])
        f2 = self._failing([self._ann(msg_b)])
        k_a = _normalize_ci_failure_reason(f1, self._summary(f1))
        k_b = _normalize_ci_failure_reason(f2, self._summary(f2))
        assert k_a != k_b

    def test_same_message_different_paths_yields_same_key(self) -> None:
        """Identical error messages should cluster regardless of
        which file/line raised them."""
        f1 = self._failing([self._ann("Incompatible types", "src/foo.py", 10)])
        f2 = self._failing([self._ann("Incompatible types", "src/bar.py", 99)])
        assert _normalize_ci_failure_reason(
            f1, self._summary(f1)
        ) == _normalize_ci_failure_reason(f2, self._summary(f2))

    def test_same_check_no_annotations_yields_stable_key(self) -> None:
        """A bare 'ci / Tests' failure with no annotations must yield
        a deterministic (not random) key."""
        f = self._failing()
        s = "## ❌ ci / Tests\n\n**Job logs:**\nlog"
        k1 = _normalize_ci_failure_reason(f, s)
        k2 = _normalize_ci_failure_reason(f, s)
        assert k1 == k2

    def test_job_log_text_does_not_affect_key(self) -> None:
        """Transient log noise (varies per run) must not change the key."""
        f = self._failing([self._ann("mypy error")])
        prefix = "## ❌ ci / Tests\n\n- mypy error\n"
        k_short = _normalize_ci_failure_reason(f, prefix + "\n**Job logs:**\nshort\n")
        k_long = _normalize_ci_failure_reason(
            f, prefix + "\n**Job logs:**\n" + "x" * 10000 + "\n"
        )
        assert k_short == k_long

    def test_key_is_16_hex_chars(self) -> None:
        f = self._failing([self._ann("some error")])
        key = _normalize_ci_failure_reason(f, self._summary(f))
        assert len(key) == 16
        assert all(c in "0123456789abcdef" for c in key)

    def test_two_check_names_produce_different_key(self) -> None:
        """When a second check (e.g. mill-specific) also fails, the key
        must differ from the single-check case."""
        f_single = self._failing([self._ann("error msg")])
        f_double = [{"name": "ci / Tests"}, {"name": "mill-specific"}]
        summary_double = "## ❌ ci / Tests\n\n## ❌ mill-specific\n\n**Job logs:**\nlog"
        k1 = _normalize_ci_failure_reason(f_single, self._summary(f_single))
        k2 = _normalize_ci_failure_reason(f_double, summary_double)
        assert k1 != k2

    def test_annotation_messages_in_summary_are_preserved(self) -> None:
        """When annotations are absent from the failing list but present
        in the summary text, those messages still influence the key."""
        f = self._failing()
        s1 = (
            "## ❌ ci / Tests\n\n**Annotations:**\n"
            "- [error] src/a.py:1: Incompatible types\n"
            "\n**Job logs:**\nlog"
        )
        s2 = (
            "## ❌ ci / Tests\n\n**Annotations:**\n"
            "- [error] src/a.py:1: unused variable F841\n"
            "\n**Job logs:**\nlog"
        )
        k1 = _normalize_ci_failure_reason(f, s1)
        k2 = _normalize_ci_failure_reason(f, s2)
        assert k1 != k2
