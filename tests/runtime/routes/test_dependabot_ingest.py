"""Tests for the Dependabot vulnerability-alert ingest poll loop."""

import asyncio
import json

from robotsix_mill.config import (
    RepoConfig,
    ReposRegistry,
    Settings,
    _reset_repos_config,
)
from robotsix_mill.core import db
from robotsix_mill.core.models import SourceKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.runtime.worker import Worker
from robotsix_mill.stages import StageContext


def _ctx(tmp_path, **env):
    db.reset_engine()
    env.setdefault("data_dir", str(tmp_path / "data"))
    env.setdefault("require_approval", "false")
    env.setdefault("FORGE_KIND", "github")
    env.setdefault("FORGE_REMOTE_URL", "https://github.com/o/r.git")
    env.setdefault("FORGE_TOKEN", "tok")
    s = Settings(**env)

    import robotsix_mill.config as _cfg
    from robotsix_mill.config import Secrets, _reset_secrets

    _reset_secrets()
    _cfg._secrets = Secrets(forge_token="tok")
    db.init_db(s, board_id="test-board")

    repo_config = RepoConfig(
        repo_id="test-repo",
        langfuse_project_name="test",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
    )
    _reset_repos_config()
    _cfg._repos_config = ReposRegistry(repos={repo_config.repo_id: repo_config})

    return StageContext(
        settings=s,
        service=TicketService(s, board_id=repo_config.repo_id),
        repo_config=repo_config,
    )


def _alert(number, *, severity="high", ghsa="GHSA-a", package="lodash"):
    return {
        "number": number,
        "ghsa_id": ghsa,
        "cve_id": "CVE-2021-1",
        "severity": severity,
        "package": package,
        "ecosystem": "npm",
        "manifest_path": "package-lock.json",
        "summary": "Prototype pollution",
        "url": f"https://github.com/o/r/security/dependabot/{number}",
    }


def _patch_forge(monkeypatch, alerts):
    class FakeForge:
        def list_dependabot_alerts(self):
            return alerts

    monkeypatch.setattr(
        "robotsix_mill.forge.get_forge",
        lambda settings, repo_config=None: FakeForge(),
    )


def test_files_one_draft_per_alert(tmp_path, monkeypatch):
    """Each open alert becomes a source='dependabot_alerts' draft."""
    ctx = _ctx(tmp_path)
    _patch_forge(
        monkeypatch,
        [
            _alert(1, ghsa="GHSA-a", package="lodash"),
            _alert(2, ghsa="GHSA-b", package="requests", severity="critical"),
        ],
    )
    worker = Worker(ctx)
    rc = ctx.repo_config

    created = asyncio.run(
        worker._poll_one_repo_dependabot(
            rc, now=1000.0, ttl_seconds=10**9, remaining_cap=10
        )
    )
    assert created == 2

    tickets = [
        t for t in ctx.service.list() if t.source == SourceKind.DEPENDABOT_ALERTS
    ]
    assert len(tickets) == 2
    titles = sorted(t.title for t in tickets)
    assert any("lodash" in t for t in titles)
    assert any("requests" in t for t in titles)


def test_dedup_skips_already_seen(tmp_path, monkeypatch):
    """A second pass over the same alerts files nothing new."""
    ctx = _ctx(tmp_path)
    _patch_forge(monkeypatch, [_alert(1, ghsa="GHSA-a", package="lodash")])
    worker = Worker(ctx)
    rc = ctx.repo_config

    first = asyncio.run(
        worker._poll_one_repo_dependabot(
            rc, now=1000.0, ttl_seconds=10**9, remaining_cap=10
        )
    )
    second = asyncio.run(
        worker._poll_one_repo_dependabot(
            rc, now=1001.0, ttl_seconds=10**9, remaining_cap=10
        )
    )
    assert first == 1
    assert second == 0
    tickets = [
        t for t in ctx.service.list() if t.source == SourceKind.DEPENDABOT_ALERTS
    ]
    assert len(tickets) == 1

    # State file records the dedup key.
    state_path = ctx.settings.data_dir / "test-repo" / "dependabot_ingest_state.json"
    state = json.loads(state_path.read_text("utf-8"))
    assert "GHSA-a:lodash" in state["seen"]


def test_per_pass_cap_defers_overflow(tmp_path, monkeypatch):
    """The remaining_cap bounds drafts; uncapped alerts are NOT marked seen."""
    ctx = _ctx(tmp_path)
    _patch_forge(
        monkeypatch,
        [
            _alert(1, ghsa="GHSA-a", package="a"),
            _alert(2, ghsa="GHSA-b", package="b"),
            _alert(3, ghsa="GHSA-c", package="c"),
        ],
    )
    worker = Worker(ctx)
    rc = ctx.repo_config

    created = asyncio.run(
        worker._poll_one_repo_dependabot(
            rc, now=1000.0, ttl_seconds=10**9, remaining_cap=2
        )
    )
    assert created == 2

    # The third alert was deferred (not seen) — a later uncapped pass files it.
    again = asyncio.run(
        worker._poll_one_repo_dependabot(
            rc, now=1001.0, ttl_seconds=10**9, remaining_cap=10
        )
    )
    assert again == 1
    tickets = [
        t for t in ctx.service.list() if t.source == SourceKind.DEPENDABOT_ALERTS
    ]
    assert len(tickets) == 3


def test_no_alerts_is_noop(tmp_path, monkeypatch):
    """An empty alert list files nothing and does not crash."""
    ctx = _ctx(tmp_path)
    _patch_forge(monkeypatch, [])
    worker = Worker(ctx)
    rc = ctx.repo_config

    created = asyncio.run(
        worker._poll_one_repo_dependabot(
            rc, now=1000.0, ttl_seconds=10**9, remaining_cap=10
        )
    )
    assert created == 0
    assert [
        t for t in ctx.service.list() if t.source == SourceKind.DEPENDABOT_ALERTS
    ] == []
