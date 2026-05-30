"""Tests for the cost-reconciliation runner."""

from robotsix_mill.cost_reconciliation_runner import (
    _fetch_openrouter_daily,
    _fetch_langfuse_daily,
    _yesterday_utc_range,
    _yesterday_date_str,
    run_cost_reconciliation_pass,
)
from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.models import SourceKind
from robotsix_mill.core.service import TicketService


def _test_repo_config():
    """Synthetic RepoConfig for periodic-runner tests — the runner now
    requires one (mono-repo board-less mode is gone)."""
    from robotsix_mill.config import RepoConfig

    return RepoConfig(
        repo_id="test-repo",
        board_id="test-board",
        langfuse_project_name="test-project",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
    )


def _make_settings(tmp_path, **overrides):
    """Create Settings with data_dir pointing to tmp_path."""
    overrides.setdefault("data_dir", str(tmp_path / "data"))
    s = Settings(**overrides)
    db.reset_engine()
    db.init_db(s, board_id="test-board")
    return s


# ---------------------------------------------------------------------------
# OpenRouter fetch
# ---------------------------------------------------------------------------


def test_fetch_openrouter_no_key_returns_none(settings, monkeypatch):
    """When openrouter_management_key is empty, returns None."""
    monkeypatch.setattr(
        "robotsix_mill.cost_reconciliation_runner.get_secrets",
        lambda: type("S", (), {"openrouter_management_key": None})(),
    )
    result = _fetch_openrouter_daily(settings, "2025-01-01")
    assert result is None


def test_fetch_openrouter_skips_on_401(settings, monkeypatch):
    """HTTP 401/403 → returns None gracefully."""
    import httpx

    class _FakeResponse:
        status_code = 401

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, headers):
            return _FakeResponse()

    monkeypatch.setattr(httpx, "Client", lambda **kw: _FakeClient())
    monkeypatch.setattr(
        "robotsix_mill.cost_reconciliation_runner.get_secrets",
        lambda: type("S", (), {"openrouter_management_key": "test-key"})(),
    )
    result = _fetch_openrouter_daily(settings, "2025-01-01")
    assert result is None


def test_fetch_openrouter_parses_data(settings, monkeypatch):
    """Valid 200 response → (total, breakdown)."""
    import httpx

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {
                "data": [
                    {
                        "model": "gpt-4",
                        "usage": 1.50,
                        "byok_usage_inference": 0.0,
                        "num_requests": 10,
                    },
                    {
                        "model": "claude-3",
                        "usage": 2.00,
                        "byok_usage_inference": 0.50,
                        "num_requests": 5,
                    },
                ]
            }

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url, headers):
            return _FakeResponse()

    monkeypatch.setattr(httpx, "Client", lambda **kw: _FakeClient())
    monkeypatch.setattr(
        "robotsix_mill.cost_reconciliation_runner.get_secrets",
        lambda: type("S", (), {"openrouter_management_key": "test-key"})(),
    )
    total, breakdown = _fetch_openrouter_daily(settings, "2025-01-01")
    assert total == 4.00  # 1.50 + 2.00 + 0.50
    assert "gpt-4" in breakdown
    assert "claude-3" in breakdown
    assert "$1.5000" in breakdown
    assert "$2.5000" in breakdown


# ---------------------------------------------------------------------------
# Langfuse fetch
# ---------------------------------------------------------------------------


def test_fetch_langfuse_aggregates_traces(settings, monkeypatch):
    """Traces are summed and grouped by name."""
    calls = []

    def fake_api_get(s, path, params=None, repo_config=None):
        calls.append(params)
        page = (params or {}).get("page", 1)
        if page == 1:
            return {
                "data": [
                    {"name": "implement", "totalCost": 1.5},
                    {"name": "refine", "totalCost": 2.0},
                ],
                "meta": {"totalPages": 1},
            }
        return {"data": [], "meta": {"totalPages": 1}}

    monkeypatch.setattr(
        "robotsix_mill.langfuse_client._langfuse_api_get",
        fake_api_get,
    )
    total, breakdown = _fetch_langfuse_daily(
        settings, "2025-01-01T00:00:00Z", "2025-01-02T00:00:00Z"
    )
    assert total == 3.5
    assert "implement" in breakdown
    assert "refine" in breakdown
    # Check that toTimestamp was passed
    assert calls[0].get("toTimestamp") == "2025-01-02T00:00:00Z"


def test_fetch_langfuse_graceful_on_error(settings, monkeypatch):
    """API error → (0.0, error message)."""

    def fake_api_get(s, path, params=None, repo_config=None):
        return None  # API failure

    monkeypatch.setattr(
        "robotsix_mill.langfuse_client._langfuse_api_get",
        fake_api_get,
    )
    total, breakdown = _fetch_langfuse_daily(
        settings, "2025-01-01T00:00:00Z", "2025-01-02T00:00:00Z"
    )
    assert total == 0.0
    assert "error" in breakdown.lower()


# ---------------------------------------------------------------------------
# Runner — full pass
# ---------------------------------------------------------------------------


def test_clean_pass_no_agent_no_ticket(tmp_path, monkeypatch):
    """Delta ≤ $1.00 → no agent call, no ticket."""
    settings = _make_settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.cost_reconciliation_runner.Settings",
        lambda: settings,
    )

    # Both sources report ~same total
    def fake_or(settings, date_str):
        return (10.00, "gpt-4: $10.00")

    def fake_lf(settings, from_ts, to_ts, repo_config=None):
        return (9.50, "implement: $9.50")

    monkeypatch.setattr(
        "robotsix_mill.cost_reconciliation_runner._fetch_openrouter_daily",
        fake_or,
    )
    monkeypatch.setattr(
        "robotsix_mill.cost_reconciliation_runner._fetch_langfuse_daily",
        fake_lf,
    )

    agent_called = []

    def fake_agent(**kwargs):
        agent_called.append(True)
        from robotsix_mill.agents.cost_reconciling import CostReconciliationResult

        return CostReconciliationResult(analysis="", conclusion="")

    monkeypatch.setattr(
        "robotsix_mill.agents.cost_reconciling.run_cost_reconciliation_agent",
        fake_agent,
    )

    result = run_cost_reconciliation_pass(repo_config=_test_repo_config())
    assert result.drafts_created == []
    assert "clean" in result.summary
    assert len(agent_called) == 0


def test_dirty_pass_creates_draft(tmp_path, monkeypatch):
    """Delta > $1.00 → agent called, draft ticket created."""
    settings = _make_settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.cost_reconciliation_runner.Settings",
        lambda: settings,
    )

    def fake_or(settings, date_str):
        return (15.00, "gpt-4: $15.00")

    def fake_lf(settings, from_ts, to_ts, repo_config=None):
        return (10.00, "implement: $10.00")

    monkeypatch.setattr(
        "robotsix_mill.cost_reconciliation_runner._fetch_openrouter_daily",
        fake_or,
    )
    monkeypatch.setattr(
        "robotsix_mill.cost_reconciliation_runner._fetch_langfuse_daily",
        fake_lf,
    )

    from robotsix_mill.agents.cost_reconciling import CostReconciliationResult

    def fake_agent(**kwargs):
        return CostReconciliationResult(
            analysis="Analysis text",
            conclusion="Needs investigation.",
        )

    monkeypatch.setattr(
        "robotsix_mill.agents.cost_reconciling.run_cost_reconciliation_agent",
        fake_agent,
    )

    result = run_cost_reconciliation_pass(repo_config=_test_repo_config())
    assert len(result.drafts_created) == 1
    draft = result.drafts_created[0]
    assert draft["id"]
    assert "Cost reconciliation" in draft["title"]
    assert "5.00" in result.summary  # delta

    # Verify the ticket exists in the DB with correct source
    service = TicketService(settings, board_id="test-board")
    ticket = service.get(draft["id"])
    assert ticket is not None
    assert ticket.source == SourceKind.COST_RECONCILIATION
    # Verify the marker is in the body
    body = service.workspace(ticket).read_description()
    assert "cost_reconciliation-gap-id" in body


def test_missing_management_key_skips_gracefully(tmp_path, monkeypatch):
    """When OpenRouter fetch returns None, pass skips cleanly."""
    settings = _make_settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.cost_reconciliation_runner.Settings",
        lambda: settings,
    )

    def fake_or(settings, date_str):
        return None  # no key

    monkeypatch.setattr(
        "robotsix_mill.cost_reconciliation_runner._fetch_openrouter_daily",
        fake_or,
    )

    result = run_cost_reconciliation_pass(repo_config=_test_repo_config())
    assert result.drafts_created == []
    assert "skip" in result.summary.lower()


def test_langfuse_error_runs_comparison(tmp_path, monkeypatch):
    """Langfuse 0.0 fallback → comparison still runs."""
    settings = _make_settings(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.cost_reconciliation_runner.Settings",
        lambda: settings,
    )

    def fake_or(settings, date_str):
        return (5.00, "gpt-4: $5.00")

    def fake_lf(settings, from_ts, to_ts, repo_config=None):
        return (0.0, "Langfuse API error — unable to fetch traces")

    monkeypatch.setattr(
        "robotsix_mill.cost_reconciliation_runner._fetch_openrouter_daily",
        fake_or,
    )
    monkeypatch.setattr(
        "robotsix_mill.cost_reconciliation_runner._fetch_langfuse_daily",
        fake_lf,
    )

    from robotsix_mill.agents.cost_reconciling import CostReconciliationResult

    def fake_agent(**kwargs):
        return CostReconciliationResult(
            analysis="",
            conclusion="Langfuse was down.",
        )

    monkeypatch.setattr(
        "robotsix_mill.agents.cost_reconciling.run_cost_reconciliation_agent",
        fake_agent,
    )

    result = run_cost_reconciliation_pass(repo_config=_test_repo_config())
    assert len(result.drafts_created) == 1  # delta > $1 → draft
    assert "5.00" in result.summary


# ---------------------------------------------------------------------------
# Dedup against prior cost-reconciliation drafts
# ---------------------------------------------------------------------------


def _patch_dirty_pass(monkeypatch, settings, delta=5.00):
    """Wire fake OpenRouter/Langfuse responses that yield *delta* > $1.00
    so the dedup gate is exercised after the clean-pass early-return."""
    monkeypatch.setattr(
        "robotsix_mill.cost_reconciliation_runner.Settings",
        lambda: settings,
    )
    monkeypatch.setattr(
        "robotsix_mill.cost_reconciliation_runner._fetch_openrouter_daily",
        lambda settings, date_str: (10.00 + delta, "gpt-4: $15"),
    )
    monkeypatch.setattr(
        "robotsix_mill.cost_reconciliation_runner._fetch_langfuse_daily",
        lambda settings, from_ts, to_ts, repo_config=None: (10.00, "implement: $10"),
    )

    from robotsix_mill.agents.cost_reconciling import CostReconciliationResult

    agent_calls: list = []

    def fake_agent(**kwargs):
        agent_calls.append(kwargs)
        return CostReconciliationResult(
            analysis="x",
            conclusion="y",
        )

    monkeypatch.setattr(
        "robotsix_mill.agents.cost_reconciling.run_cost_reconciliation_agent",
        fake_agent,
    )
    return agent_calls


def test_duplicate_date_suppressed(tmp_path, monkeypatch):
    """When a prior cost_reconciliation draft for the same date already
    exists, the runner must NOT invoke the agent and must NOT create a
    second ticket. Without dedup, every cron run on a $1+ delta day
    would clone the existing draft."""
    settings = _make_settings(tmp_path)
    agent_calls = _patch_dirty_pass(monkeypatch, settings)

    # Pre-seed a prior cost-reconciliation ticket for yesterday's date
    # with the marker that the runner would have written.
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
    assert "already filed" in result.summary
    assert prior.id in result.summary
    assert agent_calls == []  # agent never invoked


def test_new_date_creates_draft_when_prior_exists_for_other_date(
    tmp_path,
    monkeypatch,
):
    """A prior draft for a DIFFERENT date must not block today's draft."""
    settings = _make_settings(tmp_path)
    agent_calls = _patch_dirty_pass(monkeypatch, settings)

    service = TicketService(settings, board_id="test-board")
    other_date = "2025-01-01"  # any non-yesterday date
    body = f"old\n<!-- cost_reconciliation-gap-id: {other_date} -->\n"
    service.create(
        "Cost reconciliation: stale draft",
        body,
        source=SourceKind.COST_RECONCILIATION,
    )

    result = run_cost_reconciliation_pass(repo_config=_test_repo_config())

    assert len(result.drafts_created) == 1
    assert len(agent_calls) == 1  # agent invoked for the new date


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def test_yesterday_date_str_format():
    """Returns YYYY-MM-DD format."""
    date_str = _yesterday_date_str()
    parts = date_str.split("-")
    assert len(parts) == 3
    assert len(parts[0]) == 4  # year
    assert len(parts[1]) == 2  # month
    assert len(parts[2]) == 2  # day


def test_yesterday_utc_range():
    """Returns two ISO timestamps 24h apart."""
    from_ts, to_ts = _yesterday_utc_range()
    from datetime import datetime

    f = datetime.fromisoformat(from_ts.replace("Z", "+00:00"))
    t = datetime.fromisoformat(to_ts.replace("Z", "+00:00"))
    diff = (t - f).total_seconds()
    assert diff == 86400.0  # exactly 24 hours
    # to_ts should be at 00:00:00
    assert t.hour == 0
    assert t.minute == 0
    assert t.second == 0
