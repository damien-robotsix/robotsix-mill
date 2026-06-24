"""Unit tests for ``resolve_mill_service`` in ``draft_target.py``.

``resolve_mill_service`` is a read-only config lookup that resolves the
mill maintenance board's :class:`TicketService` from settings, returning
``None`` on every failure path so callers fall back explicitly. These
seam-level tests cover each error path plus the success case.

(``looks_like_mill_internal`` from the same module is covered separately
in ``tests/stages/test_retrospect_stage.py``.)
"""

import logging
from types import SimpleNamespace

from robotsix_mill.core.draft_target import (
    referenced_mill_paths_absent,
    resolve_mill_service,
)
from robotsix_mill.core.service import TicketService


def test_resolve_mill_service_unset_target_returns_none(settings, service, caplog):
    """Unset ``trace_review_target_repo_id`` → warn and return None."""
    settings.trace_review_target_repo_id = ""
    with caplog.at_level(logging.WARNING):
        result = resolve_mill_service(settings, service, caller_label="retrospect")
    assert result is None
    assert "trace_review_target_repo_id is unset" in caplog.text
    assert "retrospect" in caplog.text


def test_resolve_mill_service_lookup_exception_returns_none(
    settings, service, monkeypatch, caplog
):
    """A config-lookup exception (e.g. missing repos.yaml) is caught and
    yields None."""
    settings.trace_review_target_repo_id = "robotsix-mill"

    def _boom():
        raise RuntimeError("repos.yaml missing")

    monkeypatch.setattr("robotsix_mill.config.get_repos_config", _boom)
    with caplog.at_level(logging.ERROR):
        result = resolve_mill_service(settings, service, caller_label="audit")
    assert result is None
    assert "target-repo lookup failed" in caplog.text
    assert "audit" in caplog.text


def test_resolve_mill_service_unknown_repo_id_returns_none(
    settings, service, monkeypatch, caplog
):
    """A ``target_repo_id`` absent from the registry → warn and return None."""
    settings.trace_review_target_repo_id = "robotsix-mill"
    reg = SimpleNamespace(repos={})
    monkeypatch.setattr("robotsix_mill.config.get_repos_config", lambda: reg)
    with caplog.at_level(logging.WARNING):
        result = resolve_mill_service(settings, service)
    assert result is None
    assert "not in repos.yaml" in caplog.text


def test_resolve_mill_service_missing_board_id_returns_none(
    settings, service, monkeypatch, caplog
):
    """A target repo whose config has no ``board_id`` → warn and return None."""
    settings.trace_review_target_repo_id = "robotsix-mill"
    rc = SimpleNamespace(board_id="")
    reg = SimpleNamespace(repos={"robotsix-mill": rc})
    monkeypatch.setattr("robotsix_mill.config.get_repos_config", lambda: reg)
    with caplog.at_level(logging.WARNING):
        result = resolve_mill_service(settings, service)
    assert result is None
    assert "has no board_id" in caplog.text


def test_resolve_mill_service_success_returns_bound_service(
    settings, service, monkeypatch
):
    """A valid target repo → a fresh ``TicketService`` bound to its board."""
    settings.trace_review_target_repo_id = "robotsix-mill"
    rc = SimpleNamespace(board_id="mill-board")
    reg = SimpleNamespace(repos={"robotsix-mill": rc})
    monkeypatch.setattr("robotsix_mill.config.get_repos_config", lambda: reg)
    result = resolve_mill_service(settings, service)
    assert isinstance(result, TicketService)
    assert result.board_id == "mill-board"
    assert result is not service


# ---------------------------------------------------------------------------
# Tests for ``referenced_mill_paths_absent``
# ---------------------------------------------------------------------------


def test_referenced_mill_paths_absent_repo_dir_none_returns_empty():
    """``repo_dir`` is None → returns [] regardless of text mentioning mill paths."""
    result = referenced_mill_paths_absent(
        title="Fix src/robotsix_mill/core/notify.py",
        body="The file agent_definitions/language_instructions/python.md needs changes.",
        repo_dir=None,
    )
    assert result == []


def test_referenced_mill_paths_absent_no_mill_paths_returns_empty(tmp_path):
    """Text with no mill-prefixed paths → []."""
    result = referenced_mill_paths_absent(
        title="Fix the login bug",
        body="Update the auth module and add tests.",
        repo_dir=tmp_path,
    )
    assert result == []


def test_referenced_mill_paths_absent_mill_paths_exist_returns_empty(tmp_path):
    """Text references a mill-prefixed path that exists on disk → []."""
    file_path = tmp_path / "src/robotsix_mill/core/draft_target.py"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("")

    result = referenced_mill_paths_absent(
        title="Refactor src/robotsix_mill/core/draft_target.py",
        body="",
        repo_dir=tmp_path,
    )
    assert result == []


def test_referenced_mill_paths_absent_mill_paths_absent_returns_them(tmp_path):
    """Text references a mill-prefixed path that does NOT exist → returns it."""
    result = referenced_mill_paths_absent(
        title="Update agent_definitions/language_instructions/python.md",
        body="",
        repo_dir=tmp_path,
    )
    assert result == ["agent_definitions/language_instructions/python.md"]


def test_referenced_mill_paths_absent_mixed_present_absent(tmp_path):
    """Present and absent mill paths → returns only the absent ones."""
    (tmp_path / "src/robotsix_mill").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src/robotsix_mill/__init__.py").write_text("")

    result = referenced_mill_paths_absent(
        title="src/robotsix_mill/__init__.py exists, src/robotsix_mill/notify.py missing",
        body="",
        repo_dir=tmp_path,
    )
    assert result == ["src/robotsix_mill/notify.py"]


def test_referenced_mill_paths_absent_case_insensitive_prefix(tmp_path):
    """Uppercase mill-prefixed path is detected (prefix match is case-insensitive)."""
    result = referenced_mill_paths_absent(
        title="Check SRC/ROBOTSIX_MILL/core/test.py",
        body="",
        repo_dir=tmp_path,
    )
    assert result == ["SRC/ROBOTSIX_MILL/core/test.py"]


def test_referenced_mill_paths_absent_non_mill_prefixed_paths_ignored(tmp_path):
    """Path not starting with a mill prefix → ignored even if absent."""
    result = referenced_mill_paths_absent(
        title="Update tests/stages/test_foo.py",
        body="",
        repo_dir=tmp_path,
    )
    assert result == []


def test_referenced_mill_paths_absent_config_mill_prefix(tmp_path):
    """Path starting with ``config/mill.`` prefix and absent → returned."""
    result = referenced_mill_paths_absent(
        title="Tweak config/mill.settings.yaml",
        body="",
        repo_dir=tmp_path,
    )
    assert result == ["config/mill.settings.yaml"]


# ---------------------------------------------------------------------------
# src/-aware regression tests (resolve_under_src wired in)
# ---------------------------------------------------------------------------


def test_referenced_mill_paths_absent_src_fallback_present(tmp_path):
    """A mill-prefixed token that exists only under src/ is reported
    **present** (not absent) — the resolve_under_src fallback kicks in
    and finds it."""
    # ``agent_definitions/`` is a MILL_PATH_PREFIXES entry.
    (tmp_path / "src" / "agent_definitions" / "lang_instructions").mkdir(parents=True)
    (
        tmp_path / "src" / "agent_definitions" / "lang_instructions" / "python.md"
    ).write_text("")

    result = referenced_mill_paths_absent(
        title="agent_definitions/lang_instructions/python.md needs updating",
        body="",
        repo_dir=tmp_path,
    )
    # Must be empty — the path exists under src/.
    assert result == []


def test_referenced_mill_paths_absent_src_fallback_genuinely_absent(tmp_path):
    """A truly-absent mill-prefixed token is still reported absent
    even with src/ fallback — no regression on the absent detection."""
    result = referenced_mill_paths_absent(
        title="src/robotsix_mill/core/nonexistent_module.py",
        body="",
        repo_dir=tmp_path,
    )
    assert result == ["src/robotsix_mill/core/nonexistent_module.py"]


def test_referenced_mill_paths_absent_root_present_still_present(tmp_path):
    """A mill-prefixed token that exists at repo root (literal candidate)
    is still reported present — literal-first ordering preserved."""
    (tmp_path / "src" / "robotsix_mill" / "core").mkdir(parents=True)
    (tmp_path / "src" / "robotsix_mill" / "core" / "real_module.py").write_text("")

    result = referenced_mill_paths_absent(
        title="src/robotsix_mill/core/real_module.py",
        body="",
        repo_dir=tmp_path,
    )
    assert result == []
