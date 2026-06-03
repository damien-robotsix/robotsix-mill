"""Tests for the cost-reconciliation runner.

The runner now delegates the actual fetches to llmio's neutral seams
(``OpenRouterProviderCostSource`` / ``LangfuseCostLogSource``) and the
comparison to ``robotsix_llmio.core.reconcile`` — those are unit-tested in
llmio. Here we test the mill-side glue: the thin ``_fetch_provider_cost`` /
``_fetch_logged_cost`` adapters and the full pass policy (clean / dirty /
dedup / skip).
"""

from datetime import timezone

from robotsix_mill.cost_reconciliation_runner import (
    _fetch_logged_cost,
    _fetch_provider_cost,
    _yesterday_window,
    run_cost_reconciliation_pass,
)
from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.models import SourceKind
from robotsix_mill.core.service import TicketService


def _test_repo_config():
    from robotsix_mill.config import RepoConfig

    return RepoConfig(
        repo_id="test-repo",
        board_id="test-board",
        langfuse_project_name="test-project",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
    )


def _make_settings(tmp_path, **overrides):
    overrides.setdefault("data_dir", str(tmp_path / "data"))
    s = Settings(**overrides)
    db.reset_engine()
    db.init_db(s, board_id="test-board")
    return s


def _yesterday_date_str() -> str:
    return _yesterday_window().start.date().isoformat()


# ---------------------------------------------------------------------------
# mill-side fetch adapters (thin wrappers over the llmio sources)
# ---------------------------------------------------------------------------


def test_fetch_provider_cost_no_key_returns_none(settings, monkeypatch):
    monkeypatch.setattr(
        "robotsix_mill.cost_reconciliation_runner.get_secrets",
        lambda: type("S", (), {"openrouter_management_key": None})(),
    )
    assert _fetch_provider_cost(settings, _yesterday_window()) is None


def test_fetch_provider_cost_delegates_to_llmio(settings, monkeypatch):
    from robotsix_llmio.core.provider_cost import ProviderCost

    monkeypatch.setattr(
        "robotsix_mill.cost_reconciliation_runner.get_secrets",
        lambda: type("S", (), {"openrouter_management_key": "mgmt"})(),
    )

    class _FakeSource:
        def __init__(self, *, management_key):
            assert management_key == "mgmt"

        def fetch_provider_cost(self, window):
            return ProviderCost(
                total_cost=4.0, breakdown={"gpt-4": 1.5, "claude": 2.5}, request_count=9
            )

    import robotsix_llmio.openrouter as orpkg

    monkeypatch.setattr(orpkg, "OpenRouterProviderCostSource", _FakeSource)
    total, breakdown = _fetch_provider_cost(settings, _yesterday_window())
    assert total == 4.0
    assert "gpt-4" in breakdown and "claude" in breakdown


def test_fetch_logged_cost_no_creds(settings, monkeypatch):
    monkeypatch.setattr(
        "robotsix_mill.cost_reconciliation_runner.get_secrets",
        lambda: type(
            "S",
            (),
            {
                "langfuse_public_key": "",
                "langfuse_secret_key": "",
                "langfuse_base_url": None,
            },
        )(),
    )
    total, msg = _fetch_logged_cost(settings, _yesterday_window(), None)
    assert total == 0.0 and "credentials" in msg


def test_fetch_logged_cost_delegates_to_llmio(settings, monkeypatch):
    from datetime import datetime

    from robotsix_llmio.core.cost_log import CostRecord, LoggedCost

    class _FakeSource:
        def __init__(self, *, public_key, secret_key, base_url):
            pass

        def fetch_logged_cost(self, window):
            ts = datetime(2026, 6, 2, tzinfo=timezone.utc)
            return LoggedCost(
                total_cost=3.5,
                record_count=2,
                records=[
                    CostRecord(id="a", cost=1.5, timestamp=ts, name="implement"),
                    CostRecord(id="b", cost=2.0, timestamp=ts, name="refine"),
                ],
            )

    import robotsix_llmio.core as core

    monkeypatch.setattr(core, "LangfuseCostLogSource", _FakeSource)
    total, breakdown = _fetch_logged_cost(
        settings, _yesterday_window(), _test_repo_config()
    )
    assert total == 3.5
    assert "implement" in breakdown and "refine" in breakdown


def test_fetch_logged_cost_provider_filter_delegates(settings, monkeypatch):
    """With ``provider=`` set, the adapter calls the provider-filtered read
    (not the account-wide one) so the logged side matches the key's scope."""
    from robotsix_llmio.core.cost_log import LoggedCost

    captured = {}

    class _FakeSource:
        def __init__(self, *, public_key, secret_key, base_url):
            pass

        def fetch_logged_cost(self, window):
            raise AssertionError("should use the provider-filtered read")

        def fetch_logged_cost_by_provider(self, window, provider):
            captured["provider"] = provider
            return LoggedCost(total_cost=1.5, record_count=1, records=[])

    import robotsix_llmio.core as core

    monkeypatch.setattr(core, "LangfuseCostLogSource", _FakeSource)
    total, _ = _fetch_logged_cost(
        settings, _yesterday_window(), _test_repo_config(), provider="openrouter"
    )
    assert total == 1.5
    assert captured["provider"] == "openrouter"


# ---------------------------------------------------------------------------
# Full pass policy
# ---------------------------------------------------------------------------


def _patch_pass(monkeypatch, settings, *, or_total, lf_total, or_bd="or", lf_bd="lf"):
    monkeypatch.setattr(
        "robotsix_mill.cost_reconciliation_runner.Settings", lambda: settings
    )
    monkeypatch.setattr(
        "robotsix_mill.cost_reconciliation_runner._fetch_provider_cost",
        lambda settings, window: (or_total, or_bd),
    )
    monkeypatch.setattr(
        "robotsix_mill.cost_reconciliation_runner._fetch_logged_cost",
        lambda settings, window, repo_config: (lf_total, lf_bd),
    )


def _patch_agent(monkeypatch, *, analysis="a", conclusion="c"):
    from robotsix_mill.agents.cost_reconciling import CostReconciliationResult

    calls: list = []

    def fake_agent(**kwargs):
        calls.append(kwargs)
        return CostReconciliationResult(analysis=analysis, conclusion=conclusion)

    monkeypatch.setattr(
        "robotsix_mill.agents.cost_reconciling.run_cost_reconciliation_agent",
        fake_agent,
    )
    return calls


def test_clean_pass_no_agent_no_ticket(tmp_path, monkeypatch):
    """Delta within the $1 tolerance → no agent, no ticket."""
    settings = _make_settings(tmp_path)
    _patch_pass(monkeypatch, settings, or_total=10.0, lf_total=9.5)
    agent_calls = _patch_agent(monkeypatch)

    result = run_cost_reconciliation_pass(repo_config=_test_repo_config())
    assert result.drafts_created == []
    assert "clean" in result.summary
    assert agent_calls == []


def test_dirty_pass_creates_draft(tmp_path, monkeypatch):
    """Delta over the $1 tolerance → agent + draft ticket."""
    settings = _make_settings(tmp_path)
    _patch_pass(monkeypatch, settings, or_total=15.0, lf_total=10.0)
    _patch_agent(monkeypatch, analysis="Analysis", conclusion="Investigate")

    result = run_cost_reconciliation_pass(repo_config=_test_repo_config())
    assert len(result.drafts_created) == 1
    draft = result.drafts_created[0]
    assert "Cost reconciliation" in draft["title"]
    assert "5.00" in result.summary

    service = TicketService(settings, board_id="test-board")
    ticket = service.get(draft["id"])
    assert ticket is not None and ticket.source == SourceKind.COST_RECONCILIATION
    assert "cost_reconciliation-gap-id" in service.workspace(ticket).read_description()


def test_missing_management_key_skips_gracefully(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.cost_reconciliation_runner.Settings", lambda: settings
    )
    monkeypatch.setattr(
        "robotsix_mill.cost_reconciliation_runner._fetch_provider_cost",
        lambda settings, window: None,
    )
    result = run_cost_reconciliation_pass(repo_config=_test_repo_config())
    assert result.drafts_created == []
    assert "skip" in result.summary.lower()


def test_langfuse_error_runs_comparison(tmp_path, monkeypatch):
    """Langfuse 0.0 fallback → comparison still runs (delta > $1 → draft)."""
    settings = _make_settings(tmp_path)
    _patch_pass(
        monkeypatch,
        settings,
        or_total=5.0,
        lf_total=0.0,
        lf_bd="Langfuse API error — unable to fetch traces",
    )
    _patch_agent(monkeypatch, conclusion="Langfuse was down")
    result = run_cost_reconciliation_pass(repo_config=_test_repo_config())
    assert len(result.drafts_created) == 1
    assert "5.00" in result.summary


# ---------------------------------------------------------------------------
# Dedup against prior cost-reconciliation drafts
# ---------------------------------------------------------------------------


def test_duplicate_date_suppressed(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)
    _patch_pass(monkeypatch, settings, or_total=15.0, lf_total=10.0)
    agent_calls = _patch_agent(monkeypatch)

    service = TicketService(settings, board_id="test-board")
    date_str = _yesterday_date_str()
    body = f"old\n<!-- cost_reconciliation-gap-id: {date_str} -->\n"
    prior = service.create(
        "Cost reconciliation: prior draft",
        body,
        source=SourceKind.COST_RECONCILIATION,
    )

    result = run_cost_reconciliation_pass(repo_config=_test_repo_config())
    assert result.drafts_created == []
    assert "already filed" in result.summary and prior.id in result.summary
    assert agent_calls == []


def test_new_date_creates_draft_when_prior_exists_for_other_date(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)
    _patch_pass(monkeypatch, settings, or_total=15.0, lf_total=10.0)
    agent_calls = _patch_agent(monkeypatch)

    service = TicketService(settings, board_id="test-board")
    body = "old\n<!-- cost_reconciliation-gap-id: 2025-01-01 -->\n"
    service.create(
        "Cost reconciliation: stale draft",
        body,
        source=SourceKind.COST_RECONCILIATION,
    )

    result = run_cost_reconciliation_pass(repo_config=_test_repo_config())
    assert len(result.drafts_created) == 1
    assert len(agent_calls) == 1


# ---------------------------------------------------------------------------
# Per-key mode (per-project OpenRouter key → snapshot/diff reconcile)
# ---------------------------------------------------------------------------


def _repo_config_with_key(key="or-key"):
    from robotsix_mill.config import RepoConfig

    return RepoConfig(
        repo_id="test-repo",
        board_id="test-board",
        langfuse_project_name="p",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        openrouter_api_key=key,
    )


def _patch_key_usage(monkeypatch, usages):
    """Patch OpenRouterKeyCostSource to yield successive cumulative usages."""
    from robotsix_llmio.openrouter.provider_cost import KeyUsage

    it = iter(usages)

    class _FakeKeySrc:
        def __init__(self, *, api_key):
            pass

        def fetch_key_usage(self):
            return KeyUsage(usage=next(it))

    import robotsix_llmio.openrouter as orpkg

    monkeypatch.setattr(orpkg, "OpenRouterKeyCostSource", _FakeKeySrc)


def test_per_key_baseline_then_clean(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.cost_reconciliation_runner.Settings", lambda: settings
    )
    _patch_key_usage(monkeypatch, [10.0, 12.0])  # +$2 between runs
    monkeypatch.setattr(
        "robotsix_mill.cost_reconciliation_runner._fetch_logged_cost",
        lambda settings, window, repo_config, *, provider=None: (
            1.8,
            "lf",
        ),  # within $1 of $2
    )
    _patch_agent(monkeypatch)
    rc = _repo_config_with_key()

    r1 = run_cost_reconciliation_pass(repo_config=rc)
    assert r1.drafts_created == [] and "baseline" in r1.summary

    r2 = run_cost_reconciliation_pass(repo_config=rc)
    assert r2.drafts_created == [] and "clean (per-key" in r2.summary


def test_per_key_dirty_files_draft(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.cost_reconciliation_runner.Settings", lambda: settings
    )
    _patch_key_usage(monkeypatch, [10.0, 18.0])  # +$8 provider spend
    monkeypatch.setattr(
        "robotsix_mill.cost_reconciliation_runner._fetch_logged_cost",
        lambda settings, window, repo_config, *, provider=None: (
            2.0,
            "lf",
        ),  # logged only $2 → $6 gap
    )
    agent_calls = _patch_agent(monkeypatch)
    rc = _repo_config_with_key()

    run_cost_reconciliation_pass(repo_config=rc)  # baseline
    result = run_cost_reconciliation_pass(repo_config=rc)  # delta=$6 > $1
    assert len(result.drafts_created) == 1
    assert len(agent_calls) == 1
    assert "6.00" in result.summary or "draft" in result.summary


# ---------------------------------------------------------------------------
# window helper
# ---------------------------------------------------------------------------


def test_yesterday_window_is_one_settled_utc_day():
    w = _yesterday_window()
    assert (w.end - w.start).total_seconds() == 86400.0
    assert w.end.hour == 0 and w.end.minute == 0 and w.end.second == 0
    assert w.end.tzinfo is not None
