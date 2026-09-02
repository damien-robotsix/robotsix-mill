"""Per-repo ``meta_exclude`` must survive the trip from config to RepoConfig.

Regression guard for a fleet-wide meta-pass bug: the field was declared on
:class:`RepoConfig` but never read in :func:`load_repos_config`, so it kept
its (opt-in ``False``) default for every repo no matter what the config
said.  An operator setting ``meta_exclude: true`` in ``config/repos.yaml``
was silently dropped and the periodic meta (fleet-consistency) pass went on
cloning/studying the repo and filing META drafts against its board.  This is
the same class of bug as ``auto_merge_enabled`` (see
``tests/config/test_auto_merge_enabled_wiring.py``).
"""

from __future__ import annotations

import json

from robotsix_mill.config.repos import load_repos_config


def _write_config(tmp_path, monkeypatch, repos: dict) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"settings": {}, "repos": repos}))
    monkeypatch.setenv("ROBOTSIX_CONFIG_FILE", str(cfg))


def test_meta_exclude_defaults_false(tmp_path, monkeypatch):
    """A repo entry with no ``meta_exclude`` key is NOT excluded.

    ``False`` is the model default, so ordinary repos keep participating in
    the meta pass.
    """
    _write_config(
        tmp_path,
        monkeypatch,
        {"demo": {"board_id": "demo", "forge_remote_url": "https://x/demo"}},
    )

    assert load_repos_config().repos["demo"].meta_exclude is False


def test_meta_exclude_true_in_config_is_honoured(tmp_path, monkeypatch):
    """An explicit per-repo opt-out reaches RepoConfig.

    This is the assertion that actually fails without the wiring: before
    the fix the constructor never read the key, so the value below was
    silently dropped and the attribute reported the field default ``False``.
    """
    _write_config(
        tmp_path,
        monkeypatch,
        {
            "demo": {
                "board_id": "demo",
                "forge_remote_url": "https://x/demo",
                "meta_exclude": True,
            }
        },
    )

    assert load_repos_config().repos["demo"].meta_exclude is True


def test_meta_exclude_false_in_config_is_honoured(tmp_path, monkeypatch):
    """An explicit per-repo ``false`` is not treated as excluded."""
    _write_config(
        tmp_path,
        monkeypatch,
        {
            "demo": {
                "board_id": "demo",
                "forge_remote_url": "https://x/demo",
                "meta_exclude": False,
            }
        },
    )

    assert load_repos_config().repos["demo"].meta_exclude is False


def test_per_repo_opt_out_is_independent(tmp_path, monkeypatch):
    """One repo opting out must not exclude its siblings from the meta pass."""
    _write_config(
        tmp_path,
        monkeypatch,
        {
            "keeps": {"board_id": "keeps", "forge_remote_url": "https://x/keeps"},
            "excluded": {
                "board_id": "excluded",
                "forge_remote_url": "https://x/excluded",
                "meta_exclude": True,
            },
        },
    )

    repos = load_repos_config().repos
    assert repos["keeps"].meta_exclude is False
    assert repos["excluded"].meta_exclude is True
