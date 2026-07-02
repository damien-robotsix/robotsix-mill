"""Unit tests for stateless helpers in ci_fix_helpers.py.

Covers functions not already tested in test_ci_fix.py or test_codeql_fp_triage.py:
_format_code_scanning_alerts, _format_labelled_alerts, _format_alert_refs,
_alert_loc, _write_text, and _FailingContext.
"""

from robotsix_mill.stages.ci_fix_helpers import (
    _FailingContext,
    _alert_loc,
    _format_alert_refs,
    _format_code_scanning_alerts,
    _format_labelled_alerts,
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
