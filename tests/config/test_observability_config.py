"""Validate per-repo Langfuse observability config in ``load_repos_config``.

The public/secret key pair is the canonical "observability is configured"
signal: both present → configured, both absent → no observability (unchanged
behavior), exactly one present → ``ConfigError`` (a half-configured block that
would fail opaquely at runtime). A repo inheriting via ``langfuse_from`` carries
no own keys and must not be caught by the partial-config check.
"""

import pytest

from robotsix_mill.config import load_repos_config
from robotsix_mill.config.loader import ConfigError


def _write(tmp_path, body):
    f = tmp_path / "repos.yaml"
    f.write_text(body, encoding="utf-8")
    return str(f)


# ---------------------------------------------------------------------------
# No observability config — unchanged behavior
# ---------------------------------------------------------------------------


def test_no_langfuse_block_loads_with_empty_credentials(tmp_path):
    """A repo with no ``langfuse:`` block loads successfully and yields
    empty-string credential fields — exactly as today (no regression)."""
    body = """\
repos:
  plain:
    board_id: "plain"
    forge_remote_url: "https://github.com/me/plain.git"
"""
    reg = load_repos_config(_write(tmp_path, body))
    cfg = reg.repos["plain"]
    assert cfg.langfuse_public_key == ""
    assert cfg.langfuse_secret_key == ""
    assert cfg.langfuse_project_name == ""


# ---------------------------------------------------------------------------
# Partial config — rejected (both directions)
# ---------------------------------------------------------------------------


def test_public_key_without_secret_key_raises(tmp_path):
    body = """\
repos:
  half:
    board_id: "half"
    langfuse:
      public_key: "pk"
"""
    with pytest.raises(ConfigError, match="half.*secret_key"):
        load_repos_config(_write(tmp_path, body))


def test_secret_key_without_public_key_raises(tmp_path):
    body = """\
repos:
  half:
    board_id: "half"
    langfuse:
      secret_key: "sk"
"""
    with pytest.raises(ConfigError, match="half.*public_key"):
        load_repos_config(_write(tmp_path, body))


# ---------------------------------------------------------------------------
# Fully-configured — valid
# ---------------------------------------------------------------------------


def test_both_keys_without_project_name_loads(tmp_path):
    body = """\
repos:
  obs:
    board_id: "obs"
    langfuse:
      public_key: "pk"
      secret_key: "sk"
"""
    reg = load_repos_config(_write(tmp_path, body))
    cfg = reg.repos["obs"]
    assert cfg.langfuse_public_key == "pk"
    assert cfg.langfuse_secret_key == "sk"
    assert cfg.langfuse_project_name == ""


def test_both_keys_with_project_name_loads(tmp_path):
    body = """\
repos:
  obs:
    board_id: "obs"
    langfuse:
      project_name: "obs-proj"
      public_key: "pk"
      secret_key: "sk"
"""
    reg = load_repos_config(_write(tmp_path, body))
    cfg = reg.repos["obs"]
    assert cfg.langfuse_project_name == "obs-proj"
    assert cfg.langfuse_public_key == "pk"
    assert cfg.langfuse_secret_key == "sk"


# ---------------------------------------------------------------------------
# langfuse_from regression guard — must not over-fire
# ---------------------------------------------------------------------------


def test_langfuse_from_member_not_caught_by_partial_check(tmp_path):
    """A member inheriting via ``langfuse_from`` carries no own keys and must
    load without being rejected by the new partial-config validator."""
    body = """\
repos:
  master:
    board_id: "master"
    langfuse:
      public_key: "pk-m"
      secret_key: "sk-m"
  member:
    board_id: "member"
    langfuse_from: "master"
"""
    reg = load_repos_config(_write(tmp_path, body))
    assert reg.repos["member"].langfuse_public_key == "pk-m"
    assert reg.repos["member"].langfuse_secret_key == "sk-m"
