"""Tests for ``repo_settings.load_repo_test_command``.

The loader reads ``<repo_dir>/.robotsix-mill/config.yaml`` and returns
its ``test_command`` value (stripped) or ``None``. It must NEVER raise
on any missing/malformed input — a managed repo can't be allowed to
crash mill by committing a broken file.
"""

from __future__ import annotations

import logging

from robotsix_mill.repo_settings import load_repo_test_command


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
    with caplog.at_level(logging.WARNING, logger="robotsix_mill.repo_settings"):
        assert load_repo_test_command(tmp_path) is None
    assert any("mapping" in r.message for r in caplog.records)


def test_non_string_value_warns_and_returns_none(tmp_path, caplog):
    _write_config(tmp_path, "test_command: 123\n")
    with caplog.at_level(logging.WARNING, logger="robotsix_mill.repo_settings"):
        assert load_repo_test_command(tmp_path) is None
    assert any("string" in r.message for r in caplog.records)


def test_malformed_yaml_warns_and_returns_none(tmp_path, caplog):
    _write_config(tmp_path, "test_command: [unterminated\n")
    with caplog.at_level(logging.WARNING, logger="robotsix_mill.repo_settings"):
        # Must not raise.
        assert load_repo_test_command(tmp_path) is None
    assert any("read/parse error" in r.message for r in caplog.records)
