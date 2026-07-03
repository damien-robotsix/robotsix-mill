"""Single config entry point: repos read from the main config.json.

Repos live under the ``repos:`` key of ``config/config.json`` — the sole
on-disk source.  The standalone ``config/repos.yaml`` is not read at all
(the deprecated fallback was removed).  Zero repos is valid.
"""

from __future__ import annotations

import json

from robotsix_mill.config.loader import load_repos_yaml


def test_repos_read_from_main_config_json(tmp_path, monkeypatch):
    """The ``repos:`` section of the main config.json is authoritative."""
    monkeypatch.delenv("MILL_REPOS_FILE", raising=False)  # not the "" pin
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "settings": {},
                "repos": {
                    "demo": {
                        "board_id": "demo",
                        "forge_remote_url": "https://github.com/o/demo",
                    }
                },
            }
        )
    )
    monkeypatch.setenv("MILL_CONFIG_FILE", str(cfg))

    repos = load_repos_yaml()
    assert set(repos) == {"demo"}
    assert repos["demo"]["board_id"] == "demo"


def test_empty_repos_key_means_zero_repos(tmp_path, monkeypatch):
    """An explicit empty ``repos:`` key yields zero repos (valid)."""
    monkeypatch.delenv("MILL_REPOS_FILE", raising=False)
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "settings": {"data_dir": ".data"},
                "repos": {},
            }
        )
    )
    monkeypatch.setenv("MILL_CONFIG_FILE", str(cfg))

    assert load_repos_yaml() == {}


def test_standalone_repos_yaml_is_ignored(tmp_path, monkeypatch):
    """A standalone config/repos.yaml is dead weight — never read."""
    monkeypatch.delenv("MILL_REPOS_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "repos.yaml").write_text(
        "repos:\n"
        "  ghost:\n"
        "    board_id: ghost\n"
        "    forge_remote_url: https://github.com/o/ghost\n"
    )
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "settings": {"data_dir": ".data"},
                "repos": {},
            }
        )
    )
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


def test_overlay_entries_appear_in_merged_repos(tmp_path, monkeypatch):
    """Overlay entries are merged with operator repos from config.json."""
    monkeypatch.delenv("MILL_REPOS_FILE", raising=False)
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "settings": {"data_dir": str(data_dir)},
                "repos": {
                    "repo_a": {
                        "board_id": "board-a",
                        "forge_remote_url": "https://github.com/o/repo_a",
                    }
                },
            }
        )
    )
    monkeypatch.setenv("MILL_CONFIG_FILE", str(cfg))

    overlay = data_dir / "registered_repos.yaml"
    overlay.write_text(
        "repos:\n"
        "  repo_b:\n"
        "    board_id: board-b\n"
        "    forge_remote_url: https://github.com/o/repo_b\n"
    )

    repos = load_repos_yaml()
    assert set(repos) == {"repo_a", "repo_b"}
    assert repos["repo_a"]["board_id"] == "board-a"
    assert repos["repo_b"]["board_id"] == "board-b"


def test_operator_wins_on_repo_id_conflict(tmp_path, monkeypatch):
    """When the same repo_id appears in both config.json and overlay,
    the operator entry wins."""
    monkeypatch.delenv("MILL_REPOS_FILE", raising=False)
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "settings": {"data_dir": str(data_dir)},
                "repos": {
                    "repo_a": {
                        "board_id": "operator-board",
                        "forge_remote_url": "https://github.com/o/operator",
                    }
                },
            }
        )
    )
    monkeypatch.setenv("MILL_CONFIG_FILE", str(cfg))

    overlay = data_dir / "registered_repos.yaml"
    overlay.write_text(
        "repos:\n"
        "  repo_a:\n"
        "    board_id: auto-board\n"
        "    forge_remote_url: https://github.com/o/auto\n"
    )

    repos = load_repos_yaml()
    assert set(repos) == {"repo_a"}
    assert repos["repo_a"]["board_id"] == "operator-board"
    assert repos["repo_a"]["forge_remote_url"] == "https://github.com/o/operator"


def test_missing_overlay_tolerated(tmp_path, monkeypatch):
    """When the overlay does not exist, operator repos are returned without error."""
    monkeypatch.delenv("MILL_REPOS_FILE", raising=False)
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "settings": {"data_dir": str(data_dir)},
                "repos": {
                    "repo_a": {
                        "board_id": "board-a",
                        "forge_remote_url": "https://github.com/o/repo_a",
                    }
                },
            }
        )
    )
    monkeypatch.setenv("MILL_CONFIG_FILE", str(cfg))

    # No overlay file created — should not error.
    repos = load_repos_yaml()
    assert set(repos) == {"repo_a"}
    assert repos["repo_a"]["board_id"] == "board-a"
