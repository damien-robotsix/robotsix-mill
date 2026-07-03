"""Tests for ``repo_settings.load_repo_test_command``.

The loader reads ``<repo_dir>/.robotsix-mill/config.yaml`` and returns
its ``test_command`` value (stripped) or ``None``. It must NEVER raise
on any missing/malformed input — a managed repo can't be allowed to
crash mill by committing a broken file.
"""

from __future__ import annotations

import logging

from types import SimpleNamespace

from robotsix_mill.config.repo_settings import (
    load_extra_sandbox_packages,
    load_repo_languages,
    load_repo_skip_ci,
    load_repo_smoke_command,
    load_repo_smoke_paths,
    load_repo_test_command,
    resolve_language_instructions,
    warn_if_deprecated_log_folder,
)


def _write_config(repo_dir, text: str):
    cfg_dir = repo_dir / ".robotsix-mill"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text(text, encoding="utf-8")


def test_none_repo_dir_returns_none():
    assert load_repo_test_command(None) is None


def test_missing_file_returns_none(tmp_path):
    # No .robotsix-mill/config.yaml at all.
    assert load_repo_test_command(tmp_path) is None


def test_present_command_is_stripped(tmp_path):
    _write_config(tmp_path, 'test_command: "  pytest -q  "\n')
    assert load_repo_test_command(tmp_path) == "pytest -q"


def test_plain_command(tmp_path):
    _write_config(tmp_path, 'test_command: "pytest -q"\n')
    assert load_repo_test_command(tmp_path) == "pytest -q"


def test_empty_value_returns_none(tmp_path):
    _write_config(tmp_path, 'test_command: "   "\n')
    assert load_repo_test_command(tmp_path) is None


def test_missing_key_returns_none(tmp_path):
    _write_config(tmp_path, "other_key: value\n")
    assert load_repo_test_command(tmp_path) is None


def test_non_mapping_top_level_returns_none(tmp_path, caplog):
    _write_config(tmp_path, "- just\n- a\n- list\n")
    with caplog.at_level(logging.WARNING, logger="robotsix_mill.config.repo_settings"):
        assert load_repo_test_command(tmp_path) is None
    assert any("mapping" in r.message for r in caplog.records)


def test_non_string_value_warns_and_returns_none(tmp_path, caplog):
    _write_config(tmp_path, "test_command: 123\n")
    with caplog.at_level(logging.WARNING, logger="robotsix_mill.config.repo_settings"):
        assert load_repo_test_command(tmp_path) is None
    assert any("string" in r.message for r in caplog.records)


def test_malformed_yaml_warns_and_returns_none(tmp_path, caplog):
    _write_config(tmp_path, "test_command: [unterminated\n")
    with caplog.at_level(logging.WARNING, logger="robotsix_mill.config.repo_settings"):
        # Must not raise.
        assert load_repo_test_command(tmp_path) is None
    assert any("read/parse error" in r.message for r in caplog.records)


# --- languages -------------------------------------------------------------


def test_load_repo_languages_list(tmp_path):
    _write_config(tmp_path, "languages:\n  - python\n  - rust\n")
    assert load_repo_languages(tmp_path) == ["python", "rust"]


def test_load_repo_languages_singular_string(tmp_path):
    _write_config(tmp_path, "language: go\n")
    assert load_repo_languages(tmp_path) == ["go"]


def test_load_repo_languages_list_wins_over_singular(tmp_path):
    _write_config(tmp_path, "language: go\nlanguages: [python]\n")
    assert load_repo_languages(tmp_path) == ["python"]


def test_load_repo_languages_absent_or_malformed(tmp_path):
    _write_config(tmp_path, "test_command: pytest\n")
    assert load_repo_languages(tmp_path) == []
    _write_config(tmp_path, "languages: 123\n")
    assert load_repo_languages(tmp_path) == []
    assert load_repo_languages(None) == []


def _settings_with_builtin(tmp_path):
    builtin = tmp_path / "builtin_lang"
    builtin.mkdir()
    (builtin / "python.md").write_text("PY BUILTIN", encoding="utf-8")
    (builtin / "rust.md").write_text("RUST BUILTIN", encoding="utf-8")
    return SimpleNamespace(language_instructions_dir=builtin)


def test_resolve_languages_from_repo_file_multi(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo, "languages: [python, rust]\n")
    s = _settings_with_builtin(tmp_path)
    out = resolve_language_instructions(s, repo)
    assert "PY BUILTIN" in out and "RUST BUILTIN" in out


def test_resolve_no_repos_yaml_fallback(tmp_path):
    # A repo with no .robotsix-mill/config.yaml languages declares nothing —
    # there is NO repos.yaml `language` fallback anymore.
    repo = tmp_path / "repo"
    repo.mkdir()
    s = _settings_with_builtin(tmp_path)
    assert resolve_language_instructions(s, repo) == ""


def test_resolve_repo_override_wins_over_builtin(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo, "languages: [python]\n")
    override_dir = repo / ".robotsix-mill" / "language_instructions"
    override_dir.mkdir(parents=True)
    (override_dir / "python.md").write_text("REPO OVERRIDE", encoding="utf-8")
    s = _settings_with_builtin(tmp_path)
    out = resolve_language_instructions(s, repo)
    assert out.strip() == "REPO OVERRIDE"
    assert "PY BUILTIN" not in out


def test_resolve_none_when_no_language(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    s = _settings_with_builtin(tmp_path)
    assert resolve_language_instructions(s, repo) == ""


def test_resolve_javascript_from_builtin(tmp_path):
    """When a repo declares languages: [javascript], the built-in
    javascript.md is resolved."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_config(repo, "languages: [javascript]\n")
    s = _settings_with_builtin(tmp_path)
    # Seed the temp builtin dir with javascript.md.
    (s.language_instructions_dir / "javascript.md").write_text(
        "JS BUILTIN\n## Manifest & lockfile workflow", encoding="utf-8"
    )
    out = resolve_language_instructions(s, repo)
    assert "JS BUILTIN" in out
    assert "## Manifest & lockfile workflow" in out


def test_real_javascript_md_mentions_key_terms():
    """Sanity-check: the committed javascript.md must mention key
    JS-tooling terms so the convention can't silently regress to an
    empty/placeholder file."""
    path = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "agent_definitions"
        / "language_instructions"
        / "javascript.md"
    )
    text = path.read_text(encoding="utf-8")
    for term in ("npm install", "package.json", "npm"):
        assert term in text, f"real javascript.md must mention '{term}'"


# --- extra_sandbox_packages ------------------------------------------------


def test_extra_sandbox_packages_none_repo_dir_returns_empty():
    assert load_extra_sandbox_packages(None) == []


def test_extra_sandbox_packages_missing_file_returns_empty(tmp_path):
    assert load_extra_sandbox_packages(tmp_path) == []


def test_extra_sandbox_packages_missing_key_returns_empty(tmp_path):
    _write_config(tmp_path, "test_command: pytest\n")
    assert load_extra_sandbox_packages(tmp_path) == []


def test_extra_sandbox_packages_list_of_strings(tmp_path):
    _write_config(
        tmp_path, "extra_sandbox_packages:\n - colcon\n - ' ros-humble-ros-core '\n"
    )
    assert load_extra_sandbox_packages(tmp_path) == ["colcon", "ros-humble-ros-core"]


def test_extra_sandbox_packages_empty_list(tmp_path):
    _write_config(tmp_path, "extra_sandbox_packages: []\n")
    assert load_extra_sandbox_packages(tmp_path) == []


def test_extra_sandbox_packages_empty_strings_filtered(tmp_path):
    _write_config(tmp_path, "extra_sandbox_packages:\n - ''\n - '  '\n - colcon\n")
    assert load_extra_sandbox_packages(tmp_path) == ["colcon"]


def test_extra_sandbox_packages_non_list_warns_and_returns_empty(tmp_path, caplog):
    _write_config(tmp_path, "extra_sandbox_packages: colcon\n")
    with caplog.at_level(logging.WARNING, logger="robotsix_mill.config.repo_settings"):
        assert load_extra_sandbox_packages(tmp_path) == []
    assert any("must be a list" in r.message for r in caplog.records)


def test_extra_sandbox_packages_non_list_int_warns(tmp_path, caplog):
    _write_config(tmp_path, "extra_sandbox_packages: 42\n")
    with caplog.at_level(logging.WARNING, logger="robotsix_mill.config.repo_settings"):
        assert load_extra_sandbox_packages(tmp_path) == []
    assert any("must be a list" in r.message for r in caplog.records)


def test_extra_sandbox_packages_non_string_coerced(tmp_path):
    _write_config(tmp_path, "extra_sandbox_packages:\n - 42\n - colcon\n")
    assert load_extra_sandbox_packages(tmp_path) == ["42", "colcon"]


def test_extra_sandbox_packages_non_mapping_top_level_returns_empty(tmp_path, caplog):
    _write_config(tmp_path, "- just\n- a\n- list\n")
    with caplog.at_level(logging.WARNING, logger="robotsix_mill.config.repo_settings"):
        assert load_extra_sandbox_packages(tmp_path) == []
    assert any("mapping" in r.message for r in caplog.records)


def test_extra_sandbox_packages_malformed_yaml_returns_empty(tmp_path, caplog):
    _write_config(tmp_path, "extra_sandbox_packages: [unterminated\n")
    with caplog.at_level(logging.WARNING, logger="robotsix_mill.config.repo_settings"):
        assert load_extra_sandbox_packages(tmp_path) == []
    assert any("read/parse error" in r.message for r in caplog.records)


# --- smoke_command ---------------------------------------------------------


def test_smoke_command_none_repo_dir_returns_none():
    assert load_repo_smoke_command(None) is None


def test_smoke_command_missing_file_returns_none(tmp_path):
    assert load_repo_smoke_command(tmp_path) is None


def test_smoke_command_present_is_stripped(tmp_path):
    _write_config(tmp_path, 'smoke_command: "  scripts/smoke.sh  "\n')
    assert load_repo_smoke_command(tmp_path) == "scripts/smoke.sh"


def test_smoke_command_missing_key_returns_none(tmp_path):
    _write_config(tmp_path, "test_command: pytest\n")
    assert load_repo_smoke_command(tmp_path) is None


def test_smoke_command_empty_value_returns_none(tmp_path):
    _write_config(tmp_path, 'smoke_command: "   "\n')
    assert load_repo_smoke_command(tmp_path) is None


def test_smoke_command_non_string_warns_and_returns_none(tmp_path, caplog):
    _write_config(tmp_path, "smoke_command: 123\n")
    with caplog.at_level(logging.WARNING, logger="robotsix_mill.config.repo_settings"):
        assert load_repo_smoke_command(tmp_path) is None
    assert any("string" in r.message for r in caplog.records)


def test_smoke_command_malformed_yaml_returns_none(tmp_path, caplog):
    _write_config(tmp_path, "smoke_command: [unterminated\n")
    with caplog.at_level(logging.WARNING, logger="robotsix_mill.config.repo_settings"):
        assert load_repo_smoke_command(tmp_path) is None
    assert any("read/parse error" in r.message for r in caplog.records)


# --- smoke_paths -----------------------------------------------------------


def test_smoke_paths_none_repo_dir_returns_empty():
    assert load_repo_smoke_paths(None) == []


def test_smoke_paths_missing_file_returns_empty(tmp_path):
    assert load_repo_smoke_paths(tmp_path) == []


def test_smoke_paths_list(tmp_path):
    _write_config(
        tmp_path,
        "smoke_paths:\n  - src/runtime/**\n  - src/x/*.css\n",
    )
    assert load_repo_smoke_paths(tmp_path) == ["src/runtime/**", "src/x/*.css"]


def test_smoke_paths_missing_key_returns_empty(tmp_path):
    _write_config(tmp_path, "smoke_command: scripts/smoke.sh\n")
    assert load_repo_smoke_paths(tmp_path) == []


def test_smoke_paths_whitespace_stripped_and_empties_filtered(tmp_path):
    _write_config(tmp_path, "smoke_paths:\n  - ' src/a/** '\n  - ''\n  - '  '\n")
    assert load_repo_smoke_paths(tmp_path) == ["src/a/**"]


def test_smoke_paths_non_string_coerced(tmp_path):
    _write_config(tmp_path, "smoke_paths:\n  - 42\n  - src/a/**\n")
    assert load_repo_smoke_paths(tmp_path) == ["42", "src/a/**"]


def test_smoke_paths_non_list_warns_and_returns_empty(tmp_path, caplog):
    _write_config(tmp_path, "smoke_paths: src/runtime/**\n")
    with caplog.at_level(logging.WARNING, logger="robotsix_mill.config.repo_settings"):
        assert load_repo_smoke_paths(tmp_path) == []
    assert any("must be a list" in r.message for r in caplog.records)


def test_smoke_paths_malformed_yaml_returns_empty(tmp_path, caplog):
    _write_config(tmp_path, "smoke_paths: [unterminated\n")
    with caplog.at_level(logging.WARNING, logger="robotsix_mill.config.repo_settings"):
        assert load_repo_smoke_paths(tmp_path) == []
    assert any("read/parse error" in r.message for r in caplog.records)


# --- deployed_log_folder (deprecated repo-owned key) -----------------------
# The key now lives in mill's central config/repos.yaml (RepoConfig). The
# repo-owned key is no longer read; a committed key only triggers a
# deprecation warning.


def test_warn_if_deprecated_log_folder_none_repo_dir_no_warn(caplog):
    with caplog.at_level(logging.WARNING, logger="robotsix_mill.config.repo_settings"):
        warn_if_deprecated_log_folder(None)
    assert not any("deployed_log_folder" in r.message for r in caplog.records)


def test_warn_if_deprecated_log_folder_missing_file_no_warn(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="robotsix_mill.config.repo_settings"):
        warn_if_deprecated_log_folder(tmp_path)
    assert not any("deployed_log_folder" in r.message for r in caplog.records)


def test_warn_if_deprecated_log_folder_absent_key_no_warn(tmp_path, caplog):
    _write_config(tmp_path, "test_command: pytest\n")
    with caplog.at_level(logging.WARNING, logger="robotsix_mill.config.repo_settings"):
        warn_if_deprecated_log_folder(tmp_path)
    assert not any("deployed_log_folder" in r.message for r in caplog.records)


def test_warn_if_deprecated_log_folder_present_key_warns(tmp_path, caplog):
    _write_config(tmp_path, "deployed_log_folder: /var/log/app\n")
    with caplog.at_level(logging.WARNING, logger="robotsix_mill.config.repo_settings"):
        warn_if_deprecated_log_folder(tmp_path)
    assert any(
        "deployed_log_folder" in r.message and "deprecated" in r.message
        for r in caplog.records
    )


# --- skip_ci ---------------------------------------------------------------


def test_skip_ci_none_repo_dir_returns_false():
    assert load_repo_skip_ci(None) is False


def test_skip_ci_missing_file_returns_false(tmp_path):
    assert load_repo_skip_ci(tmp_path) is False


def test_skip_ci_missing_key_returns_false_no_warning(tmp_path, caplog):
    _write_config(tmp_path, "test_command: pytest\n")
    with caplog.at_level(logging.WARNING, logger="robotsix_mill.config.repo_settings"):
        assert load_repo_skip_ci(tmp_path) is False
    assert not any("skip_ci" in r.message for r in caplog.records)


def test_skip_ci_true(tmp_path):
    _write_config(tmp_path, "skip_ci: true\n")
    assert load_repo_skip_ci(tmp_path) is True


def test_skip_ci_false(tmp_path):
    _write_config(tmp_path, "skip_ci: false\n")
    assert load_repo_skip_ci(tmp_path) is False


def test_skip_ci_non_bool_string_warns_and_returns_false(tmp_path, caplog):
    _write_config(tmp_path, 'skip_ci: "yes"\n')
    with caplog.at_level(logging.WARNING, logger="robotsix_mill.config.repo_settings"):
        assert load_repo_skip_ci(tmp_path) is False
    assert any("bool" in r.message for r in caplog.records)


def test_skip_ci_non_bool_int_warns_and_returns_false(tmp_path, caplog):
    _write_config(tmp_path, "skip_ci: 1\n")
    with caplog.at_level(logging.WARNING, logger="robotsix_mill.config.repo_settings"):
        assert load_repo_skip_ci(tmp_path) is False
    assert any("bool" in r.message for r in caplog.records)


def test_skip_ci_malformed_yaml_returns_false(tmp_path, caplog):
    _write_config(tmp_path, "skip_ci: [unterminated\n")
    with caplog.at_level(logging.WARNING, logger="robotsix_mill.config.repo_settings"):
        assert load_repo_skip_ci(tmp_path) is False
    assert any("read/parse error" in r.message for r in caplog.records)


def test_skip_ci_non_mapping_top_level_returns_false(tmp_path, caplog):
    _write_config(tmp_path, "- just\n- a\n- list\n")
    with caplog.at_level(logging.WARNING, logger="robotsix_mill.config.repo_settings"):
        assert load_repo_skip_ci(tmp_path) is False
    assert any("mapping" in r.message for r in caplog.records)
