"""A failed forge description update must not be recorded as a silent ``ok``.

2026-10-22: every board's repo_description_sync pass logged
``update failed (API returned False)`` yet the run registry recorded
``ok``, so the broken update path went unnoticed fleet-wide.  The runner
now raises on a failed update so the periodic registry records ``error``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from robotsix_mill.agents import base as agents_base
from robotsix_mill.agents import retry as agents_retry
from robotsix_mill.agents.runners import repo_description_sync_runner as mod
from robotsix_mill.forge.base import NotConfiguredError


class _FakeForge:
    def __init__(self, update_result):
        self._update_result = update_result
        self.calls: list[dict] = []

    def get_repo_description(self, *, owner, repo):
        return ""

    def update_repo(self, *, owner, repo, description):
        self.calls.append({"owner": owner, "repo": repo, "description": description})
        if isinstance(self._update_result, Exception):
            raise self._update_result
        return self._update_result


def _setup(monkeypatch, tmp_path: Path, forge: _FakeForge) -> SimpleNamespace:
    def fake_clone(remote_url, dest, branch, token=None, **_kw):
        Path(dest).mkdir(parents=True, exist_ok=True)
        (Path(dest) / "README.md").write_text(
            "# robotsix-http\n\nShared HTTP retry/backoff library.\n"
        )

    monkeypatch.setattr(mod.git_ops, "clone", fake_clone)
    monkeypatch.setattr(mod, "target_branch_for", lambda _s, _r: "main")
    monkeypatch.setattr(mod, "_clone_token", lambda _s, _r: "tok")
    monkeypatch.setattr(
        mod,
        "Settings",
        lambda: SimpleNamespace(forge_remote_url="", data_dir=tmp_path),
    )
    monkeypatch.setattr(mod, "get_forge", lambda _s, repo_config=None: forge)
    monkeypatch.setattr(
        agents_base, "build_agent_from_definition", lambda *_a, **_k: object()
    )
    monkeypatch.setattr(agents_base, "_safe_close", lambda _a: None)
    output = SimpleNamespace(
        should_update=True,
        new_description="Shared HTTP retry/backoff library.",
        summary="forge description empty → proposing update",
    )
    monkeypatch.setattr(
        agents_retry, "run_agent", lambda *_a, **_k: SimpleNamespace(output=output)
    )
    return SimpleNamespace(
        repo_id="robotsix-http",
        forge_remote_url="https://github.com/damien-robotsix/robotsix-http.git",
    )


def test_update_returning_false_raises(monkeypatch, tmp_path: Path) -> None:
    forge = _FakeForge(False)
    repo_config = _setup(monkeypatch, tmp_path, forge)

    with pytest.raises(RuntimeError, match="update failed"):
        mod.run_repo_description_sync_pass(
            "sess", repo_config, definition_override=object()
        )
    assert len(forge.calls) == 1


def test_update_raising_raises(monkeypatch, tmp_path: Path) -> None:
    forge = _FakeForge(ValueError("boom"))
    repo_config = _setup(monkeypatch, tmp_path, forge)

    with pytest.raises(RuntimeError, match="boom"):
        mod.run_repo_description_sync_pass(
            "sess", repo_config, definition_override=object()
        )


def test_update_success_is_ok(monkeypatch, tmp_path: Path) -> None:
    forge = _FakeForge(True)
    repo_config = _setup(monkeypatch, tmp_path, forge)

    result = mod.run_repo_description_sync_pass(
        "sess", repo_config, definition_override=object()
    )
    assert result.description_updated is True
    assert result.summary.startswith("updated:")


def test_updates_disabled_is_not_an_error(monkeypatch, tmp_path: Path) -> None:
    forge = _FakeForge(NotConfiguredError("Repo metadata updates are disabled"))
    repo_config = _setup(monkeypatch, tmp_path, forge)

    result = mod.run_repo_description_sync_pass(
        "sess", repo_config, definition_override=object()
    )
    assert result.description_updated is False
    assert result.summary.startswith("skipped: repo metadata updates disabled")
