"""Unit tests for ``robotsix_mill.vcs.git_ops``.

Most tests use a real on-disk git repo under tmp_path because the
helpers are thin wrappers over ``git`` subprocesses — mocking
subprocess.run would only test the mocks. A few tests
(``_authed_url``, pure-string helpers) are unit-tested without I/O.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from robotsix_mill.vcs import git_ops


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _init_repo(path: Path, initial_branch: str = "main") -> Path:
    """Create a fresh git repo at *path* with one commit on *initial_branch*."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "-b", initial_branch, str(path)], check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@x"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "test"],
        check=True,
    )
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "initial"],
        check=True,
    )
    return path


# ---------------------------------------------------------------------------
# _authed_url — pure string helper, security-sensitive
# ---------------------------------------------------------------------------


def test_authed_url_injects_token_into_https():
    out = git_ops._authed_url("https://github.com/owner/repo", "tok123")
    assert out == "https://oauth2:tok123@github.com/owner/repo"


def test_authed_url_no_token_returns_unchanged():
    assert git_ops._authed_url(
        "https://github.com/owner/repo", None,
    ) == "https://github.com/owner/repo"
    assert git_ops._authed_url(
        "https://github.com/owner/repo", "",
    ) == "https://github.com/owner/repo"


def test_authed_url_leaves_non_https_untouched():
    """ssh:// and file:// URLs MUST NOT get token injected — token
    would either land in the wrong place or expose the credential."""
    assert git_ops._authed_url(
        "git@github.com:owner/repo.git", "tok",
    ) == "git@github.com:owner/repo.git"
    assert git_ops._authed_url(
        "file:///tmp/repo", "tok",
    ) == "file:///tmp/repo"
    assert git_ops._authed_url(
        "ssh://git@host/repo", "tok",
    ) == "ssh://git@host/repo"


def test_authed_url_only_replaces_scheme_once():
    """``replace(..., 1)`` — don't substitute every occurrence."""
    # Pathological URL where the literal "https://" appears in the path.
    out = git_ops._authed_url(
        "https://host/url-with-https://-inside", "t",
    )
    assert out.count("oauth2:t@") == 1


# ---------------------------------------------------------------------------
# has_changes / branch_exists / head_sha
# ---------------------------------------------------------------------------


def test_has_changes_returns_false_on_clean_repo(tmp_path):
    repo = _init_repo(tmp_path / "r")
    assert git_ops.has_changes(repo) is False


def test_has_changes_returns_true_after_modify(tmp_path):
    repo = _init_repo(tmp_path / "r")
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    assert git_ops.has_changes(repo) is True


def test_has_changes_returns_true_for_new_untracked(tmp_path):
    repo = _init_repo(tmp_path / "r")
    (repo / "newfile.txt").write_text("x", encoding="utf-8")
    assert git_ops.has_changes(repo) is True


def test_branch_exists_true_for_existing(tmp_path):
    repo = _init_repo(tmp_path / "r")
    assert git_ops.branch_exists(repo, "main") is True


def test_branch_exists_false_for_missing(tmp_path):
    repo = _init_repo(tmp_path / "r")
    assert git_ops.branch_exists(repo, "does-not-exist") is False


def test_head_sha_returns_full_hash(tmp_path):
    repo = _init_repo(tmp_path / "r")
    sha = git_ops.head_sha(repo)
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


# ---------------------------------------------------------------------------
# checkout / create_branch
# ---------------------------------------------------------------------------


def test_create_branch_and_checkout(tmp_path):
    repo = _init_repo(tmp_path / "r")
    git_ops.create_branch(repo, "feature/x")
    assert git_ops.branch_exists(repo, "feature/x")
    git_ops.checkout(repo, "feature/x")
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert out == "feature/x"


# ---------------------------------------------------------------------------
# commit_all
# ---------------------------------------------------------------------------


def test_commit_all_creates_commit_with_message(tmp_path):
    repo = _init_repo(tmp_path / "r")
    (repo / "f.txt").write_text("hi", encoding="utf-8")
    git_ops.commit_all(repo, "my message")
    assert git_ops.has_changes(repo) is False
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--pretty=%s"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert log == "my message"


def test_commit_all_no_changes_is_noop(tmp_path):
    """commit_all on a clean repo must not raise — the implement stage
    relies on a no-op when the agent made no edits."""
    repo = _init_repo(tmp_path / "r")
    # Should not raise; whether it creates an empty commit is impl detail.
    try:
        git_ops.commit_all(repo, "noop")
    except subprocess.CalledProcessError:
        pass  # acceptable: git commit returns non-zero on empty commit


# ---------------------------------------------------------------------------
# clone — uses local file:// remote
# ---------------------------------------------------------------------------


def test_clone_creates_dest_with_single_branch(tmp_path):
    upstream = _init_repo(tmp_path / "upstream")
    dest = tmp_path / "clone"
    git_ops.clone(f"file://{upstream}", dest, "main")
    assert (dest / ".git").is_dir()
    # Single-branch clone: only `main` is tracked.
    assert git_ops.branch_exists(dest, "main")


def test_clone_sets_user_email_and_name(tmp_path):
    upstream = _init_repo(tmp_path / "upstream")
    dest = tmp_path / "clone"
    git_ops.clone(f"file://{upstream}", dest, "main")
    email = subprocess.run(
        ["git", "-C", str(dest), "config", "user.email"],
        capture_output=True, text=True,
    ).stdout.strip()
    name = subprocess.run(
        ["git", "-C", str(dest), "config", "user.name"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert email == "mill@robotsix.local"
    assert name == "robotsix-mill"


# ---------------------------------------------------------------------------
# changed_files — diff + untracked union
# ---------------------------------------------------------------------------


def test_changed_files_includes_untracked_and_modified(tmp_path):
    upstream = _init_repo(tmp_path / "upstream")
    dest = tmp_path / "clone"
    git_ops.clone(f"file://{upstream}", dest, "main")
    git_ops.create_branch(dest, "feature/x")
    git_ops.checkout(dest, "feature/x")
    (dest / "README.md").write_text("modified\n", encoding="utf-8")
    (dest / "newfile.txt").write_text("new\n", encoding="utf-8")
    files = git_ops.changed_files(dest, "main")
    assert "README.md" in files
    assert "newfile.txt" in files


# ---------------------------------------------------------------------------
# try_rebase_onto — token-aware fetch
# ---------------------------------------------------------------------------


def test_try_rebase_onto_no_args_uses_origin(tmp_path):
    """Backward compat: when called without remote_url/token, the
    function still uses the clone's stored origin (the legacy path)."""
    upstream = _init_repo(tmp_path / "upstream")
    dest = tmp_path / "clone"
    git_ops.clone(f"file://{upstream}", dest, "main")
    # Upstream advances → rebase becomes meaningful but still no-op
    # if the dest is already up to date.
    assert git_ops.try_rebase_onto(dest, "main") is True


def test_try_rebase_onto_uses_explicit_remote_when_given(tmp_path):
    """When remote_url + token are passed, fetch goes to the explicit
    URL (via _authed_url) rather than relying on origin's stored URL.
    Critical for the GitHub App token path: a clone made hours ago
    carries an expired token in origin; without this knob the fetch
    401s, try_rebase_onto returns False, and implement→rebase loops
    every poll (ff45 on 2026-05-29)."""
    upstream = _init_repo(tmp_path / "upstream")
    dest = tmp_path / "clone"
    git_ops.clone(f"file://{upstream}", dest, "main")
    # Break the clone's stored origin so a stored-origin fetch would
    # fail. The explicit remote_url is the only working route.
    subprocess.run(
        ["git", "-C", str(dest), "remote", "set-url", "origin",
         "file:///does/not/exist"],
        check=True,
    )
    # No token (file:// remote), but explicit remote_url should still
    # be honoured.
    assert git_ops.try_rebase_onto(
        dest, "main",
        remote_url=f"file://{upstream}",
        token=None,
    ) is True
    # ... and the stored origin URL stays broken — proving the
    # function didn't fall back to it.
    out = subprocess.run(
        ["git", "-C", str(dest), "remote", "get-url", "origin"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert out == "file:///does/not/exist"


def test_try_rebase_onto_token_is_passed_to_authed_url(tmp_path, monkeypatch):
    """When BOTH remote_url and token are given, _authed_url builds
    the credential-bearing URL once at call time — even if origin's
    stored token is expired, this run uses the fresh one."""
    captured: dict = {}
    real_run = subprocess.run

    def spy(argv, *a, **kw):
        if isinstance(argv, list) and len(argv) > 3 and argv[3] == "fetch":
            captured["fetch_args"] = list(argv)
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(git_ops.subprocess, "run", spy)

    upstream = _init_repo(tmp_path / "upstream")
    dest = tmp_path / "clone"
    git_ops.clone(f"file://{upstream}", dest, "main")
    git_ops.try_rebase_onto(
        dest, "main",
        remote_url="https://github.com/o/r",
        token="tok-fresh",
    )
    # The fetch should target the token-authed URL — NOT "origin".
    fetch_args = captured.get("fetch_args") or []
    assert "origin" not in fetch_args
    assert any("oauth2:tok-fresh@github.com/o/r" in a for a in fetch_args)
