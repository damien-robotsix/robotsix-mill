"""The repo-description-sync clone must use the per-repo forge credential.

2026-09-07: the hexarchy pass cloned with the static ``forge_token``
secret, which is not installed on that (private) repo, and skipped with
``fatal: could not read Username for 'https://github.com'``.  Every other
periodic runner resolves a per-repo token via ``_clone_token`` (GitHub App
installation token / GitLab PAT); this runner must too.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from robotsix_mill.agents.runners import repo_description_sync_runner as mod


def test_clone_uses_per_repo_clone_token(monkeypatch, tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def fake_clone(remote_url, dest, branch, token=None, **_kw):
        seen["token"] = token
        raise subprocess.CalledProcessError(128, ["git", "clone"], stderr="auth")

    monkeypatch.setattr(mod.git_ops, "clone", fake_clone)
    monkeypatch.setattr(mod, "target_branch_for", lambda _s, _r: "main")
    monkeypatch.setattr(mod, "_clone_token", lambda _s, _r: "app-installation-token")
    monkeypatch.setattr(
        mod,
        "Settings",
        lambda: SimpleNamespace(forge_remote_url="", data_dir=tmp_path),
    )
    repo_config = SimpleNamespace(
        repo_id="hexarchy",
        forge_remote_url="https://github.com/damien-robotsix/hexarchy.git",
    )

    result = mod.run_repo_description_sync_pass("sess", repo_config)

    assert seen["token"] == "app-installation-token"
    assert result.summary.startswith("skipped: clone failed")
