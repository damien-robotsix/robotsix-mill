"""Single config entry point: repos read from the main config.yaml.

Repos live under the ``repos:`` key of ``config/config.yaml`` — the sole
on-disk source.  The standalone ``config/repos.yaml`` is not read at all
(the deprecated fallback was removed).  Zero repos is valid.
"""

from __future__ import annotations

from robotsix_mill.config.loader import load_repos_yaml


def test_repos_read_from_main_config_yaml(tmp_path, monkeypatch):
    """The ``repos:`` section of the main config.yaml is authoritative."""
    monkeypatch.delenv("MILL_REPOS_FILE", raising=False)  # not the "" pin
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "repos:\n"
        "  demo:\n"
        "    board_id: demo\n"
        "    forge_remote_url: https://github.com/o/demo\n"
    )
    monkeypatch.setenv("MILL_CONFIG_FILE", str(cfg))

    repos = load_repos_yaml()
    assert set(repos) == {"demo"}
    assert repos["demo"]["board_id"] == "demo"


def test_empty_repos_key_means_zero_repos(tmp_path, monkeypatch):
    """An explicit empty ``repos:`` key yields zero repos (valid)."""
    monkeypatch.delenv("MILL_REPOS_FILE", raising=False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("repos: {}\ncore:\n  data_dir: .data\n")
    monkeypatch.setenv("MILL_CONFIG_FILE", str(cfg))

    assert load_repos_yaml() == {}


def test_standalone_repos_yaml_is_ignored(tmp_path, monkeypatch):
    """A standalone config/repos.yaml is dead weight — never read."""
    monkeypatch.delenv("MILL_REPOS_FILE", raising=False)
    monkeypatch.chdir(tmp_path)  # _YAML_DIR is cwd-relative
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "repos.yaml").write_text(
        "repos:\n"
        "  ghost:\n"
        "    board_id: ghost\n"
        "    forge_remote_url: https://github.com/o/ghost\n"
    )
    cfg = tmp_path / "config.yaml"
    cfg.write_text("core:\n  data_dir: .data\n")  # no repos: key
    monkeypatch.setenv("MILL_CONFIG_FILE", str(cfg))

    assert load_repos_yaml() == {}


def test_explicit_repos_file_still_overrides(tmp_path, monkeypatch):
    """A MILL_REPOS_FILE override still reads that file directly (tests)."""
    monkeypatch.delenv("MILL_CONFIG_FILE", raising=False)
    override = tmp_path / "repos.yaml"
    override.write_text(
        "repos:\n"
        "  leg:\n"
        "    board_id: leg\n"
        "    forge_remote_url: https://github.com/o/leg\n"
    )
    monkeypatch.setenv("MILL_REPOS_FILE", str(override))

    assert set(load_repos_yaml()) == {"leg"}
