"""The top-level ``langfuse`` block is the ONE place Langfuse is configured:
``load_repos_config`` populates every repo and the meta board from it. There
is no per-repo Langfuse config — a ``langfuse`` block on an individual repo
entry is ignored."""

from robotsix_mill.config import load_repos_config

_GLOBAL = """\
langfuse:
  project_name: "robotsix-mill"
  public_key: "pk-global"
  secret_key: "sk-global"
  base_url: "https://lf.example.com"
repos:
  bare:
    board_id: "bare"
  has_stale_block:
    board_id: "has_stale_block"
    langfuse:
      project_name: "ignored-proj"
      public_key: "pk-ignored"
      secret_key: "sk-ignored"
"""


def _write(tmp_path, body):
    f = tmp_path / "repos.yaml"
    f.write_text(body, encoding="utf-8")
    return str(f)


def test_global_block_configures_every_repo(tmp_path):
    reg = load_repos_config(_write(tmp_path, _GLOBAL))
    for rid in ("bare", "has_stale_block"):
        r = reg.repos[rid]
        assert r.langfuse_public_key == "pk-global"
        assert r.langfuse_secret_key == "sk-global"
        assert r.langfuse_project_name == "robotsix-mill"
        assert r.langfuse_base_url == "https://lf.example.com"


def test_per_repo_langfuse_block_is_ignored(tmp_path):
    """A leftover per-repo ``langfuse`` block does NOT override the global."""
    reg = load_repos_config(_write(tmp_path, _GLOBAL))
    assert reg.repos["has_stale_block"].langfuse_public_key == "pk-global"
    assert reg.repos["has_stale_block"].langfuse_project_name == "robotsix-mill"


def test_meta_board_uses_global(tmp_path):
    reg = load_repos_config(_write(tmp_path, _GLOBAL))
    assert reg.meta is not None
    assert reg.meta.langfuse_public_key == "pk-global"
    assert reg.meta.langfuse_project_name == "robotsix-mill"


def test_no_global_block_means_observability_off(tmp_path):
    body = "repos:\n  bare:\n    board_id: bare\n"
    reg = load_repos_config(_write(tmp_path, body))
    assert reg.repos["bare"].langfuse_public_key == ""
    assert reg.meta is None
