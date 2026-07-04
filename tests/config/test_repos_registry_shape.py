"""``load_repos_config`` must accept BOTH the nested ``ReposRegistry``
shape (``{"meta": ..., "repos": {...}}``) that the deploy JSON schema
models and the central-deploy onboard writes, AND the legacy flat
``{repo_id: cfg}`` shape used by hand-maintained desktop configs.

Regression for the onboard crash: a fresh onboard wrote
``repos = {"meta": null, "repos": {}}`` into ``config.json`` and the
serve path (``get_repos_config`` → ``load_repos_config``) iterated the
mapping as if it were flat, treating the literal keys ``"meta"`` /
``"repos"`` as repo IDs and building ``RepoConfig(repo_id="repos",
board_id="")`` → ``ValidationError`` → the process exited on every
start.
"""

from __future__ import annotations

import json

import robotsix_mill.config as cfg
from robotsix_mill.config import load_repos_config
from robotsix_mill.config.repos import _split_registry_shape


# ---------------------------------------------------------------------------
#  _split_registry_shape — pure normalisation unit tests
# ---------------------------------------------------------------------------


def test_split_empty_is_no_repos():
    assert _split_registry_shape({}) == ({}, None)


def test_split_nested_empty_registry():
    assert _split_registry_shape({"meta": None, "repos": {}}) == ({}, None)


def test_split_nested_repos_only():
    assert _split_registry_shape({"repos": {}}) == ({}, None)


def test_split_nested_with_repo():
    raw = {"meta": None, "repos": {"foo": {"board_id": "foo"}}}
    assert _split_registry_shape(raw) == ({"foo": {"board_id": "foo"}}, None)


def test_split_nested_carries_meta():
    raw = {"meta": {"board_id": "meta"}, "repos": {}}
    repos_mapping, meta_raw = _split_registry_shape(raw)
    assert repos_mapping == {}
    assert meta_raw == {"board_id": "meta"}


def test_split_flat_single():
    raw = {"foo": {"board_id": "foo"}}
    assert _split_registry_shape(raw) == (raw, None)


def test_split_flat_with_repo_named_repos_stays_flat():
    """A flat mapping that merely *contains* a repo named ``repos``
    alongside others must NOT be mistaken for the nested registry form."""
    raw = {"foo": {"board_id": "foo"}, "repos": {"board_id": "repos"}}
    assert _split_registry_shape(raw) == (raw, None)


# ---------------------------------------------------------------------------
#  load_repos_config end-to-end via the real config.json (onboard) path
# ---------------------------------------------------------------------------


def _write_config(tmp_path, monkeypatch, repos_value):
    """Point the loader at a config.json whose top-level ``repos`` key is
    *repos_value*, exactly as the onboard writes it."""
    monkeypatch.delenv("MILL_REPOS_FILE", raising=False)
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    conf = tmp_path / "config.json"
    conf.write_text(
        json.dumps({"settings": {"data_dir": str(data_dir)}, "repos": repos_value})
    )
    monkeypatch.setenv("MILL_CONFIG_FILE", str(conf))
    cfg._reset_repos_config()


def test_onboard_nested_empty_does_not_crash(tmp_path, monkeypatch):
    """The exact onboard payload ``{"meta": null, "repos": {}}`` loads as
    zero repos instead of crash-looping the process."""
    _write_config(tmp_path, monkeypatch, {"meta": None, "repos": {}})
    reg = load_repos_config()
    assert reg.repos == {}


def test_nested_repos_only_does_not_crash(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"repos": {}})
    reg = load_repos_config()
    assert reg.repos == {}


def test_empty_repos_key_does_not_crash(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {})
    reg = load_repos_config()
    assert reg.repos == {}


def test_nested_registry_with_repo_loads(tmp_path, monkeypatch):
    _write_config(
        tmp_path,
        monkeypatch,
        {
            "meta": None,
            "repos": {
                "foo": {
                    "board_id": "foo",
                    "forge_remote_url": "https://github.com/o/foo",
                }
            },
        },
    )
    reg = load_repos_config()
    assert set(reg.repos) == {"foo"}
    assert reg.repos["foo"].repo_id == "foo"


def test_legacy_flat_still_loads(tmp_path, monkeypatch):
    """The currently-deployed desktop FLAT shape must keep loading."""
    _write_config(
        tmp_path,
        monkeypatch,
        {
            "foo": {
                "board_id": "foo",
                "forge_remote_url": "https://github.com/o/foo",
            }
        },
    )
    reg = load_repos_config()
    assert set(reg.repos) == {"foo"}
    assert reg.repos["foo"].repo_id == "foo"


def test_flat_and_nested_same_repo_are_equivalent(tmp_path, monkeypatch):
    """The flat and nested forms carrying the same repo produce equivalent
    registries."""
    repo = {
        "board_id": "foo",
        "forge_remote_url": "https://github.com/o/foo",
        "max_concurrency": 2,
    }

    _write_config(tmp_path, monkeypatch, {"foo": dict(repo)})
    flat_reg = load_repos_config()

    _write_config(tmp_path, monkeypatch, {"meta": None, "repos": {"foo": dict(repo)}})
    nested_reg = load_repos_config()

    assert flat_reg.repos == nested_reg.repos
    assert flat_reg.repos["foo"].max_concurrency == 2
