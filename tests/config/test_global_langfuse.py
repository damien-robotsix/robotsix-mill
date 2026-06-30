"""Langfuse is configured in ONE place — the ``langfuse_*`` keys in the
config.yaml ``secrets:`` block (``Secrets``). ``load_repos_config`` populates every repo and
the meta board from them. There is no per-repo Langfuse config — a
``langfuse`` block on an individual repo entry is ignored."""

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


def test_global_secrets_configure_every_repo(tmp_path, secrets_set):
    secrets_set(
        langfuse_public_key="pk-global",
        langfuse_secret_key="sk-global",
        langfuse_project_name="robotsix-mill",
        langfuse_base_url="https://lf.example.com",
    )
    reg = load_repos_config(_write(tmp_path, _REPOS))
    for rid in ("bare", "has_stale_block"):
        r = reg.repos[rid]
        assert r.langfuse_public_key == "pk-global"
        assert r.langfuse_secret_key == "sk-global"
        assert r.langfuse_project_name == "robotsix-mill"
        assert r.langfuse_base_url == "https://lf.example.com"


def test_per_repo_langfuse_block_is_ignored(tmp_path, secrets_set):
    """A leftover per-repo ``langfuse`` block does NOT override the global."""
    secrets_set(langfuse_public_key="pk-global", langfuse_secret_key="sk-global")
    reg = load_repos_config(_write(tmp_path, _REPOS))
    assert reg.repos["has_stale_block"].langfuse_public_key == "pk-global"


def test_meta_board_uses_global(tmp_path, secrets_set):
    secrets_set(
        langfuse_public_key="pk-global",
        langfuse_secret_key="sk-global",
        langfuse_project_name="robotsix-mill",
    )
    reg = load_repos_config(_write(tmp_path, _REPOS))
    assert reg.meta is not None
    assert reg.meta.langfuse_public_key == "pk-global"
    assert reg.meta.langfuse_project_name == "robotsix-mill"


def test_no_secrets_means_observability_off(tmp_path):
    # No secrets injected → langfuse off (conftest sets MILL_SECRETS_FILE="").
    reg = load_repos_config(_write(tmp_path, "repos:\n  bare:\n    board_id: bare\n"))
    assert reg.repos["bare"].langfuse_public_key == ""
    assert reg.meta is None


def test_only_public_key_in_secrets_is_off(tmp_path, secrets_set):
    """Both keys are required; a lone public key → observability off."""
    secrets_set(langfuse_public_key="pk-only")
    reg = load_repos_config(_write(tmp_path, "repos:\n  bare:\n    board_id: bare\n"))
    assert reg.repos["bare"].langfuse_public_key == ""
    assert reg.meta is None
