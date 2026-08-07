"""Langfuse is configured in ONE place — the canonical ``langfuse``
top-level block (robotsix-standards#189). ``load_repos_config`` populates
every repo and the meta board from it. There is no per-repo Langfuse
config — a ``langfuse`` block on an individual repo entry is ignored."""

import json
import os

from robotsix_mill.config import load_repos_config

_REPOS = """\
repos:
  bare:
    board_id: "bare"
  has_stale_block:
    board_id: "has_stale_block"
    langfuse:
      public_key: "pk-ignored"
      secret_key: "sk-ignored"
"""


def _write(tmp_path, body):
    f = tmp_path / "repos.yaml"
    f.write_text(body, encoding="utf-8")
    return str(f)


def _set_langfuse_block(tmp_path, **kw):
    """Write a flat config file so ``load_config(Settings)`` works
    (Settings has ``extra="forbid"`` — no nested ``settings`` key)."""
    host = kw.pop("host", "https://langfuse.robotsix.net")
    public_key = kw.pop("public_key", "")
    secret_key = kw.pop("secret_key", "")
    project_id = kw.pop("project_id", "")
    project_name = kw.pop("project_name", "robotsix-mill")
    cfg = {
        "data_dir": str(tmp_path),
        "langfuse": {
            "host": host,
            "projects": {
                project_name: {
                    "public_key": public_key,
                    "secret_key": secret_key,
                    "project_id": project_id,
                }
            },
        },
    }
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    os.environ["ROBOTSIX_CONFIG_FILE"] = str(cfg_path)
    # Reset the repos config cache so load_repos_config picks up the new file.
    from robotsix_mill.config import _reset_repos_config

    _reset_repos_config()


def test_global_langfuse_configure_every_repo(tmp_path):
    _set_langfuse_block(
        tmp_path,
        public_key="pk-global",
        secret_key="sk-global",
        project_name="robotsix-mill",
        host="https://lf.example.com",
    )
    reg = load_repos_config(_write(tmp_path, _REPOS))
    for rid in ("bare", "has_stale_block"):
        r = reg.repos[rid]
        assert r.langfuse_public_key == "pk-global"
        assert r.langfuse_secret_key == "sk-global"
        assert r.langfuse_project_name == "robotsix-mill"
        assert r.langfuse_base_url == "https://lf.example.com"


def test_per_repo_langfuse_block_is_ignored(tmp_path):
    """A leftover per-repo ``langfuse`` block does NOT override the global."""
    _set_langfuse_block(
        tmp_path,
        public_key="pk-global",
        secret_key="sk-global",
        project_name="robotsix-mill",
    )
    reg = load_repos_config(_write(tmp_path, _REPOS))
    assert reg.repos["has_stale_block"].langfuse_public_key == "pk-global"


def test_meta_board_uses_global(tmp_path):
    _set_langfuse_block(
        tmp_path,
        public_key="pk-global",
        secret_key="sk-global",
        project_name="robotsix-mill",
    )
    reg = load_repos_config(_write(tmp_path, _REPOS))
    assert reg.meta is not None
    assert reg.meta.langfuse_public_key == "pk-global"
    assert reg.meta.langfuse_project_name == "robotsix-mill"


def test_no_langfuse_block_means_observability_off(tmp_path):
    # No langfuse block injected → langfuse off.
    cfg = {"data_dir": str(tmp_path)}
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    os.environ["ROBOTSIX_CONFIG_FILE"] = str(cfg_path)
    from robotsix_mill.config import _reset_repos_config

    _reset_repos_config()
    reg = load_repos_config(_write(tmp_path, "repos:\n  bare:\n    board_id: bare\n"))
    assert reg.repos["bare"].langfuse_public_key == ""
    assert reg.meta is None


def test_only_public_key_in_langfuse_block_is_off(tmp_path):
    """Both keys are required; a lone public key → observability off."""
    _set_langfuse_block(
        tmp_path,
        public_key="pk-only",
        project_name="robotsix-mill",
    )
    reg = load_repos_config(_write(tmp_path, "repos:\n  bare:\n    board_id: bare\n"))
    assert reg.repos["bare"].langfuse_public_key == ""
    assert reg.meta is None
