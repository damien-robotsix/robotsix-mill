"""Per-repo ``auto_merge_enabled`` must survive the trip from config to RepoConfig.

Regression guard for a fleet-wide stall: the field was declared on
:class:`RepoConfig` but never read in :func:`load_repos_config`, so it kept
its (then opt-in ``False``) default for every repo no matter what the config
said.  Gate 3 of ``_auto_merge_eligible`` was therefore unsatisfiable and
every green, review-approved PR bounced back to ``HUMAN_MR_APPROVAL``
forever — 38 tickets wedged across 18 repos before it was caught.
"""

from __future__ import annotations

import json

from robotsix_mill.config.repos import load_repos_config


def _write_config(tmp_path, monkeypatch, repos: dict) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"settings": {}, "repos": repos}))
    monkeypatch.setenv("ROBOTSIX_CONFIG_FILE", str(cfg))


def test_auto_merge_enabled_defaults_true_for_machine_registered_repo(
    tmp_path, monkeypatch
):
    """A repo entry with no ``auto_merge_enabled`` key auto-merges.

    The runtime registration path writes only ``board_id`` /
    ``forge_remote_url``, so the default is what almost every real repo
    gets.  An opt-in default here is what stalled the whole fleet.
    """
    _write_config(
        tmp_path,
        monkeypatch,
        {"demo": {"board_id": "demo", "forge_remote_url": "https://x/demo"}},
    )

    assert load_repos_config().repos["demo"].auto_merge_enabled is True


def test_auto_merge_enabled_false_in_config_is_honoured(tmp_path, monkeypatch):
    """An explicit per-repo opt-out reaches RepoConfig.

    This is the assertion that actually fails without the wiring: before
    the fix the constructor never read the key, so the value below was
    silently dropped and the attribute reported the field default.
    """
    _write_config(
        tmp_path,
        monkeypatch,
        {
            "demo": {
                "board_id": "demo",
                "forge_remote_url": "https://x/demo",
                "auto_merge_enabled": False,
            }
        },
    )

    assert load_repos_config().repos["demo"].auto_merge_enabled is False


def test_auto_merge_enabled_true_in_config_is_honoured(tmp_path, monkeypatch):
    """An explicit per-repo opt-in reaches RepoConfig."""
    _write_config(
        tmp_path,
        monkeypatch,
        {
            "demo": {
                "board_id": "demo",
                "forge_remote_url": "https://x/demo",
                "auto_merge_enabled": True,
            }
        },
    )

    assert load_repos_config().repos["demo"].auto_merge_enabled is True


def test_per_repo_opt_out_is_independent(tmp_path, monkeypatch):
    """One repo opting out must not disable auto-merge for its siblings."""
    _write_config(
        tmp_path,
        monkeypatch,
        {
            "keeps": {"board_id": "keeps", "forge_remote_url": "https://x/keeps"},
            "opted-out": {
                "board_id": "opted-out",
                "forge_remote_url": "https://x/opted-out",
                "auto_merge_enabled": False,
            },
        },
    )

    repos = load_repos_config().repos
    assert repos["keeps"].auto_merge_enabled is True
    assert repos["opted-out"].auto_merge_enabled is False
