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
    _is_spec_descriptive_path,
    has_unverifiable_cross_repo_refs,
    referenced_local_deliverable_paths,
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
    """A target repo whose config has no ``repo_id`` → warn and return None."""
    settings.trace_review_target_repo_id = "robotsix-mill"
    rc = SimpleNamespace(repo_id="")
    reg = SimpleNamespace(repos={"robotsix-mill": rc})
    monkeypatch.setattr("robotsix_mill.config.get_repos_config", lambda: reg)
    with caplog.at_level(logging.WARNING):
        result = resolve_mill_service(settings, service)
    assert result is None
    assert "has no repo_id" in caplog.text


def test_resolve_mill_service_success_returns_bound_service(
    settings, service, monkeypatch
):
    """A valid target repo → a fresh ``TicketService`` bound to its board."""
    settings.trace_review_target_repo_id = "robotsix-mill"
    rc = SimpleNamespace(repo_id="mill-board")
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
    """``config/config.example.json`` is a spec-descriptive path —
    classified as conceptual, not a source-tree path → excluded."""
    result = referenced_mill_paths_absent(
        title="Tweak config/config.example.json",
        body="",
        repo_dir=tmp_path,
    )
    assert result == []


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


# ---------------------------------------------------------------------------
# Tests for ``referenced_mill_paths_absent`` — out-of-scope exclusion
# ---------------------------------------------------------------------------


def test_referenced_mill_paths_absent_excludes_out_of_scope_heading(tmp_path):
    """A mill path appearing ONLY under ``## Explicitly out of scope``
    is excluded → returns []."""
    body = (
        "## Scope\n\n"
        "Create src/robotsix_llmio/core/sqlite_utils.py\n\n"
        "## Explicitly out of scope\n\n"
        "src/robotsix_mill/core/db.py is NOT modified here.\n"
    )
    result = referenced_mill_paths_absent(
        title="Extract SQLite helpers",
        body=body,
        repo_dir=tmp_path,
    )
    # The only mill path is under an out-of-scope heading → excluded.
    assert result == []


def test_referenced_mill_paths_absent_excludes_inline_out_of_scope_marker(tmp_path):
    """A mill path appearing ONLY under an inline out-of-scope marker
    is excluded → returns []."""
    body = (
        "## Scope\n\n"
        "Create src/robotsix_llmio/core/sqlite_utils.py\n\n"
        "**Explicitly out of scope — consumer migrations:** "
        "src/robotsix_mill/core/db.py\n"
    )
    result = referenced_mill_paths_absent(
        title="Extract SQLite helpers",
        body=body,
        repo_dir=tmp_path,
    )
    assert result == []


def test_referenced_mill_paths_absent_still_returns_in_scope_mill_paths(tmp_path):
    """A mill path in an in-scope section is still returned (absent from disk)."""
    body = (
        "## Scope\n\n"
        "Migrate agent_definitions/language_instructions/python.md\n\n"
        "## Out of scope\n\n"
        "Do NOT touch src/robotsix_mill/core/other.py\n"
    )
    result = referenced_mill_paths_absent(
        title="Update language instructions",
        body=body,
        repo_dir=tmp_path,
    )
    assert result == ["agent_definitions/language_instructions/python.md"]


# ---------------------------------------------------------------------------
# Tests for ``referenced_mill_paths_absent`` — gitignore filtering
# ---------------------------------------------------------------------------


def test_referenced_mill_paths_absent_gitignored_path_excluded(tmp_path, monkeypatch):
    """An absent mill-prefixed path that is gitignored → excluded from result."""
    # Simulate git check-ignore returning the path as ignored.
    monkeypatch.setattr(
        "robotsix_mill.core.draft_target.git_ops.ignored_paths",
        lambda _repo, paths: ["agent_definitions/missing.md"],
    )
    result = referenced_mill_paths_absent(
        title="Update agent_definitions/missing.md",
        body="",
        repo_dir=tmp_path,
    )
    assert result == []


def test_referenced_mill_paths_absent_non_gitignored_still_returned(
    tmp_path, monkeypatch
):
    """An absent mill-prefixed path NOT gitignored → still returned."""
    monkeypatch.setattr(
        "robotsix_mill.core.draft_target.git_ops.ignored_paths",
        lambda _repo, paths: [],
    )
    result = referenced_mill_paths_absent(
        title="Update agent_definitions/missing.md",
        body="",
        repo_dir=tmp_path,
    )
    assert result == ["agent_definitions/missing.md"]


def test_referenced_mill_paths_absent_mixed_gitignored_and_not(tmp_path, monkeypatch):
    """Mixed gitignored and non-gitignored absent paths → only non-gitignored."""
    monkeypatch.setattr(
        "robotsix_mill.core.draft_target.git_ops.ignored_paths",
        lambda _repo, paths: ["agent_definitions/ignored.md"],
    )
    result = referenced_mill_paths_absent(
        title=(
            "Update agent_definitions/ignored.md and src/robotsix_mill/core/kept.py"
        ),
        body="",
        repo_dir=tmp_path,
    )
    assert "src/robotsix_mill/core/kept.py" in result
    assert "agent_definitions/ignored.md" not in result
    assert len(result) == 1


def test_referenced_mill_paths_absent_git_check_ignore_fails_open(
    tmp_path, monkeypatch
):
    """If git check-ignore errors → all absent paths returned (fail-open)."""

    def _raise(*_args, **_kwargs):
        raise RuntimeError("git not found")

    monkeypatch.setattr(
        "robotsix_mill.core.draft_target.git_ops.ignored_paths",
        _raise,
    )
    result = referenced_mill_paths_absent(
        title="Update agent_definitions/missing.md",
        body="",
        repo_dir=tmp_path,
    )
    assert result == ["agent_definitions/missing.md"]


def test_referenced_mill_paths_absent_config_yaml_ignored_scenario(
    tmp_path, monkeypatch
):
    """The specific ticket scenario: ``config/config.yaml`` is absent and
    gitignored → excluded (no false-positive consumer-migration ticket)."""
    monkeypatch.setattr(
        "robotsix_mill.core.draft_target.git_ops.ignored_paths",
        lambda _repo, paths: ["config/config.yaml"],
    )
    result = referenced_mill_paths_absent(
        title="Fix config/config.yaml",
        body="",
        repo_dir=tmp_path,
    )
    assert result == []


# ---------------------------------------------------------------------------
# Tests for ``referenced_local_deliverable_paths``
# ---------------------------------------------------------------------------


def test_referenced_local_deliverable_paths_repo_dir_none_returns_empty():
    """``repo_dir`` is None → returns []."""
    result = referenced_local_deliverable_paths(
        title="Create src/robotsix_llmio/core/foo.py",
        body="",
        repo_dir=None,
    )
    assert result == []


def test_referenced_local_deliverable_paths_existing_package(tmp_path):
    """A path whose package root exists on disk → returned."""
    # Create the package directory.
    (tmp_path / "src" / "robotsix_llmio").mkdir(parents=True)
    result = referenced_local_deliverable_paths(
        title="Create src/robotsix_llmio/core/sqlite_utils.py",
        body="",
        repo_dir=tmp_path,
    )
    assert result == ["src/robotsix_llmio/core/sqlite_utils.py"]


def test_referenced_local_deliverable_paths_excludes_mill_prefixed(tmp_path):
    """Mill-prefixed paths (e.g. ``src/robotsix_mill/…``) are excluded
    from local deliverables even if the directory happens to exist."""
    # Create the mill package directory just to be sure.
    (tmp_path / "src" / "robotsix_mill").mkdir(parents=True)
    result = referenced_local_deliverable_paths(
        title="Fix src/robotsix_mill/core/db.py",
        body="",
        repo_dir=tmp_path,
    )
    # Mill-prefixed → excluded, even though the package dir exists.
    assert result == []


def test_referenced_local_deliverable_paths_excludes_agent_definitions(tmp_path):
    """``agent_definitions/…`` paths are mill-prefixed → excluded."""
    (tmp_path / "agent_definitions").mkdir(parents=True, exist_ok=True)
    result = referenced_local_deliverable_paths(
        title="Update agent_definitions/triage.yaml",
        body="",
        repo_dir=tmp_path,
    )
    assert result == []


def test_referenced_local_deliverable_paths_package_root_not_exist(tmp_path):
    """A path whose package root does NOT exist on disk → excluded."""
    # Do NOT create the package directory.
    result = referenced_local_deliverable_paths(
        title="Create src/nonexistent_pkg/core/foo.py",
        body="",
        repo_dir=tmp_path,
    )
    assert result == []


def test_referenced_local_deliverable_paths_bare_src_not_treated_as_local(tmp_path):
    """A stray ``src/foo.py``-style token (only one segment under src/)
    is NOT treated as a local deliverable (requires ≥2 segments)."""
    # First call with no src/ dir — should return [].
    _first = referenced_local_deliverable_paths(
        title="Fix src/foo.py",
        body="",
        repo_dir=tmp_path,
    )
    assert _first == []
    # Create src/ to make sure the "≥2 segments" guard works:
    # even when src/ exists, src/foo.py is still excluded because
    # the heuristic requires src/<segment>/... (≥2 segments).
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    result2 = referenced_local_deliverable_paths(
        title="Fix src/foo.py",
        body="",
        repo_dir=tmp_path,
    )
    # Still excluded: the heuristic requires src/<segment>/...
    assert result2 == []


def test_referenced_local_deliverable_paths_excludes_out_of_scope(tmp_path):
    """Paths under out-of-scope markers are excluded from local deliverables."""
    (tmp_path / "src" / "robotsix_llmio").mkdir(parents=True)
    body = (
        "## Scope\n\n"
        "Create src/robotsix_llmio/core/sqlite_utils.py\n\n"
        "## Out of scope\n\n"
        "src/robotsix_llmio/core/other.py is not touched.\n"
    )
    result = referenced_local_deliverable_paths(
        title="Extract SQLite helpers",
        body=body,
        repo_dir=tmp_path,
    )
    assert "src/robotsix_llmio/core/sqlite_utils.py" in result
    assert "src/robotsix_llmio/core/other.py" not in result


def test_referenced_local_deliverable_paths_dedup_first_seen_order(tmp_path):
    """Results are de-duplicated, preserving first-seen order."""
    (tmp_path / "src" / "robotsix_llmio").mkdir(parents=True)
    body = (
        "Create src/robotsix_llmio/core/a.py\n"
        "Also src/robotsix_llmio/core/b.py\n"
        "And again src/robotsix_llmio/core/a.py\n"
    )
    result = referenced_local_deliverable_paths(
        title="Multiple files",
        body=body,
        repo_dir=tmp_path,
    )
    assert result == [
        "src/robotsix_llmio/core/a.py",
        "src/robotsix_llmio/core/b.py",
    ]


def test_referenced_local_deliverable_paths_non_src_package(tmp_path):
    """A non-src/ path whose first segment exists as a directory → returned."""
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    result = referenced_local_deliverable_paths(
        title="Add tests/core/test_foo.py",
        body="",
        repo_dir=tmp_path,
    )
    assert result == ["tests/core/test_foo.py"]


# ---------------------------------------------------------------------------
# Tests for ``has_unverifiable_cross_repo_refs``
# ---------------------------------------------------------------------------


def test_has_unverifiable_cross_repo_refs_none_repo_dir_returns_false():
    """repo_dir=None → returns False (no filesystem to check)."""
    assert not has_unverifiable_cross_repo_refs(
        title="Wire build_refdocs_tools in create_agent_from_settings",
        body="Add src/robotsix_chat/chat/server/app.py build_refdocs_tools call.",
        repo_dir=None,
    )


def test_has_unverifiable_cross_repo_refs_all_local_paths_returns_false(tmp_path):
    """All referenced paths have package roots present in repo_dir → False."""
    (tmp_path / "src" / "robotsix_llmio").mkdir(parents=True)
    assert not has_unverifiable_cross_repo_refs(
        title="Fix src/robotsix_llmio/core/foo.py",
        body="Also update src/robotsix_llmio/config.py",
        repo_dir=tmp_path,
    )


def test_has_unverifiable_cross_repo_refs_cross_repo_package_returns_true(tmp_path):
    """A path referencing a package root not in repo_dir → True."""
    (tmp_path / "src" / "robotsix_llmio").mkdir(parents=True)
    assert has_unverifiable_cross_repo_refs(
        title="Wire build_refdocs_tools",
        body="Add build_refdocs_tools call in src/robotsix_chat/chat/server/app.py",
        repo_dir=tmp_path,
    )


def test_has_unverifiable_cross_repo_refs_mill_paths_excluded(tmp_path):
    """Mill-prefixed paths are excluded from cross-repo detection."""
    # Only the local package exists; no mill dir.
    (tmp_path / "src" / "robotsix_llmio").mkdir(parents=True)
    # src/robotsix_mill/... is mill-prefixed → excluded, so no cross-repo hit.
    assert not has_unverifiable_cross_repo_refs(
        title="Fix mill pipeline",
        body="Update src/robotsix_mill/core/draft_target.py logic.",
        repo_dir=tmp_path,
    )


def test_has_unverifiable_cross_repo_refs_agent_definitions_excluded(tmp_path):
    """agent_definitions/ paths are mill-prefixed → excluded."""
    (tmp_path / "src" / "robotsix_llmio").mkdir(parents=True)
    assert not has_unverifiable_cross_repo_refs(
        title="Update agent prompts",
        body="Revise agent_definitions/retrospect.yaml.",
        repo_dir=tmp_path,
    )


def test_has_unverifiable_cross_repo_refs_mixed_local_and_cross(tmp_path):
    """Mixed local and cross-repo paths → True (cross-repo found)."""
    (tmp_path / "src" / "robotsix_llmio").mkdir(parents=True)
    assert has_unverifiable_cross_repo_refs(
        title="Wire new module",
        body="src/robotsix_llmio/core/new_module.py added; "
        "need to wire in src/robotsix_chat/chat/server/app.py",
        repo_dir=tmp_path,
    )


def test_has_unverifiable_cross_repo_refs_out_of_scope_excluded(tmp_path):
    """Cross-repo paths in out-of-scope sections are excluded → False."""
    (tmp_path / "src" / "robotsix_llmio").mkdir(parents=True)
    body = (
        "## Scope\n\n"
        "Wire src/robotsix_llmio/core/new_module.py\n\n"
        "## Out of scope\n\n"
        "src/robotsix_chat/chat/server/app.py consumer wiring.\n"
    )
    assert not has_unverifiable_cross_repo_refs(
        title="Wire new module",
        body=body,
        repo_dir=tmp_path,
    )


def test_has_unverifiable_cross_repo_refs_bare_filename_ignored(tmp_path):
    """A bare filename (no directory component) is not treated as cross-repo."""
    (tmp_path / "src" / "robotsix_llmio").mkdir(parents=True)
    assert not has_unverifiable_cross_repo_refs(
        title="Fix foo.py",
        body="Update bar.py too.",
        repo_dir=tmp_path,
    )


def test_has_unverifiable_cross_repo_refs_empty_title_body_returns_false(tmp_path):
    """Empty title and body → False."""
    assert not has_unverifiable_cross_repo_refs(
        title=None,
        body=None,
        repo_dir=tmp_path,
    )


# ---------------------------------------------------------------------------
# Tests for ``_is_spec_descriptive_path``
# ---------------------------------------------------------------------------


def test_is_spec_descriptive_path_source_tree_prefixes_return_false():
    """Paths starting with src/, tests/, docs/, agent_definitions/
    are never classified as spec-descriptive."""
    assert not _is_spec_descriptive_path("src/robotsix_mill/core/foo.py")
    assert not _is_spec_descriptive_path("tests/core/test_foo.py")
    assert not _is_spec_descriptive_path("docs/guide.md")
    assert not _is_spec_descriptive_path("agent_definitions/refine.yaml")


def test_is_spec_descriptive_path_bare_config_yaml():
    """Bare config/config.yaml and config/config.example.json are conceptual."""
    assert _is_spec_descriptive_path("config/config.yaml")
    assert _is_spec_descriptive_path("config/config.example.json")
    assert _is_spec_descriptive_path("CONFIG/CONFIG.YAML")


def test_is_spec_descriptive_path_config_under_src_not_conceptual():
    """config/config.yaml under src/ is a source-tree path, not conceptual."""
    assert not _is_spec_descriptive_path("src/robotsix_mill/config/config.yaml")
    assert not _is_spec_descriptive_path("src/myapp/config/config.example.json")


def test_is_spec_descriptive_path_absolute_paths():
    """Absolute filesystem paths are conceptual."""
    assert _is_spec_descriptive_path("/etc/nginx/nginx.conf")
    assert _is_spec_descriptive_path("/app/config.yaml")
    assert _is_spec_descriptive_path("~/config.yaml")
    assert _is_spec_descriptive_path("./local/config.yaml")
    assert _is_spec_descriptive_path("../parent/config.yaml")


def test_is_spec_descriptive_path_container_paths():
    """Container / host-filesystem paths are conceptual."""
    assert _is_spec_descriptive_path("/app/src/server.py")
    assert _is_spec_descriptive_path("/data/config.yaml")
    assert _is_spec_descriptive_path("some/path/app/config.yaml")  # contains /app/
    assert _is_spec_descriptive_path("some/data/file.txt")  # contains /data/


def test_is_spec_descriptive_path_template_files():
    """Template files (.example.yaml, .env.example) are conceptual."""
    assert _is_spec_descriptive_path("config/config.example.json")
    assert _is_spec_descriptive_path(".env.example")
    assert _is_spec_descriptive_path("path/to/settings.example.json")


def test_is_spec_descriptive_path_compose_files():
    """Docker Compose files are conceptual."""
    assert _is_spec_descriptive_path("compose.yaml")
    assert _is_spec_descriptive_path("compose.yml")
    assert _is_spec_descriptive_path("docker-compose.yaml")
    assert _is_spec_descriptive_path("docker-compose.yml")


def test_is_spec_descriptive_path_regular_source_paths():
    """Regular source-tree paths are not conceptual."""
    assert not _is_spec_descriptive_path("src/robotsix_mill/core/draft_target.py")
    assert not _is_spec_descriptive_path("tests/core/test_something.py")
    assert not _is_spec_descriptive_path("src/robotsix_llmio/core/sqlite_utils.py")
