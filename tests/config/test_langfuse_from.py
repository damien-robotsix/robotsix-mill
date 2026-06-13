"""Validate ``langfuse_from`` reference resolution and operator rules.

A repo with ``langfuse_from: "<master>"`` inherits the master's Langfuse
project at config-load time; the four credential fields are copied in and
the reference is preserved for provenance. Operator rules forbid separate
keys, unknown references, and chained/self references.
"""

import pytest

from robotsix_mill.config import load_repos_config
from robotsix_mill.config.loader import ConfigError

_BASE_REPO = """\
repos:
  example:
    board_id: "example-board"
    forge_remote_url: "https://github.com/me/example.git"
    langfuse:
      public_key: "pk"
      secret_key: "sk"
"""

_MASTER_MEMBER = """\
repos:
  master:
    board_id: "master"
    forge_remote_url: "https://github.com/org/master.git"
    langfuse:
      project_name: "workspace-proj"
      public_key: "pk-master"
      secret_key: "sk-master"
      base_url: "https://lf.example.com"
  member:
    board_id: "member"
    forge_remote_url: "https://github.com/org/member.git"
    langfuse_from: "master"
"""


def _write(tmp_path, body):
    f = tmp_path / "repos.yaml"
    f.write_text(body, encoding="utf-8")
    return str(f)


# ---------------------------------------------------------------------------
# Resolution round-trip
# ---------------------------------------------------------------------------


def test_langfuse_from_resolves_credentials(tmp_path):
    """A member with ``langfuse_from: "master"`` gets the master's
    four Langfuse fields copied in, and keeps langfuse_from for provenance."""
    reg = load_repos_config(_write(tmp_path, _MASTER_MEMBER))
    master = reg.repos["master"]
    member = reg.repos["member"]

    assert member.langfuse_project_name == master.langfuse_project_name
    assert member.langfuse_public_key == master.langfuse_public_key
    assert member.langfuse_secret_key == master.langfuse_secret_key
    assert member.langfuse_base_url == master.langfuse_base_url
    assert member.langfuse_from == "master"


def test_langfuse_from_preserves_master_unchanged(tmp_path):
    """The master itself is not altered by being referenced."""
    reg = load_repos_config(_write(tmp_path, _MASTER_MEMBER))
    master = reg.repos["master"]
    assert master.langfuse_project_name == "workspace-proj"
    assert master.langfuse_public_key == "pk-master"
    assert master.langfuse_secret_key == "sk-master"
    assert master.langfuse_base_url == "https://lf.example.com"
    assert master.langfuse_from is None


# ---------------------------------------------------------------------------
# Unknown reference
# ---------------------------------------------------------------------------


def test_langfuse_from_unknown_raises(tmp_path):
    body = """\
repos:
  orphan:
    board_id: "orphan"
    forge_remote_url: "https://github.com/org/orphan.git"
    langfuse_from: "no-such-repo"
"""
    with pytest.raises(ConfigError, match="no-such-repo"):
        load_repos_config(_write(tmp_path, body))


# ---------------------------------------------------------------------------
# Operator rule — must not carry own keys
# ---------------------------------------------------------------------------


def test_langfuse_from_rejects_own_public_key(tmp_path):
    body = """\
repos:
  master:
    board_id: "master"
    langfuse:
      public_key: "pk-m"
      secret_key: "sk-m"
  cheater:
    board_id: "cheater"
    langfuse_from: "master"
    langfuse:
      public_key: "pk-c"
"""
    with pytest.raises(ConfigError, match="cheater"):
        load_repos_config(_write(tmp_path, body))


def test_langfuse_from_rejects_own_secret_key(tmp_path):
    body = """\
repos:
  master:
    board_id: "master"
    langfuse:
      public_key: "pk-m"
      secret_key: "sk-m"
  cheater:
    board_id: "cheater"
    langfuse_from: "master"
    langfuse:
      secret_key: "sk-c"
"""
    with pytest.raises(ConfigError, match="cheater"):
        load_repos_config(_write(tmp_path, body))


def test_langfuse_from_rejects_own_project_name(tmp_path):
    body = """\
repos:
  master:
    board_id: "master"
    langfuse:
      public_key: "pk-m"
      secret_key: "sk-m"
  cheater:
    board_id: "cheater"
    langfuse_from: "master"
    langfuse:
      project_name: "proj-c"
"""
    with pytest.raises(ConfigError, match="cheater"):
        load_repos_config(_write(tmp_path, body))


def test_langfuse_from_allows_base_url_only(tmp_path):
    """A harmless ``base_url`` alone (no project_name / public_key / secret_key)
    does NOT count as carrying separate keys."""
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
    langfuse:
      base_url: "https://cloud.langfuse.com"
"""
    reg = load_repos_config(_write(tmp_path, body))
    assert reg.repos["member"].langfuse_public_key == "pk-m"


# ---------------------------------------------------------------------------
# No chaining / self-reference
# ---------------------------------------------------------------------------


def test_langfuse_from_rejects_chaining(tmp_path):
    body = """\
repos:
  master:
    board_id: "master"
    langfuse:
      public_key: "pk-m"
      secret_key: "sk-m"
  mid:
    board_id: "mid"
    langfuse_from: "master"
  leaf:
    board_id: "leaf"
    langfuse_from: "mid"
"""
    with pytest.raises(ConfigError, match="mid"):
        load_repos_config(_write(tmp_path, body))


def test_langfuse_from_self_reference_rejected(tmp_path):
    """A repo that references itself is naturally caught by the no-chaining
    rule (langfuse_from is set on the referenced entry)."""
    body = """\
repos:
  solo:
    board_id: "solo"
    langfuse_from: "solo"
"""
    with pytest.raises(ConfigError, match="solo"):
        load_repos_config(_write(tmp_path, body))
