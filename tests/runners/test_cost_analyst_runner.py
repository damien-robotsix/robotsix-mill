"""Tests for the cost-analyst runner — deterministic digest builders +
cross-repo aggregation + draft dedup."""

from types import SimpleNamespace

from robotsix_mill.config import Settings
from robotsix_mill.runners import cost_analyst_runner as car


def _settings(**kw):
    kw.setdefault("data_dir", "/tmp/cost-analyst-test")
    return Settings(**kw)


def _repo(repo_id, keyed=True):
    return SimpleNamespace(
        repo_id=repo_id,
        board_id=repo_id,
        langfuse_public_key="pk" if keyed else "",
        langfuse_secret_key="sk" if keyed else "",
    )


# --- pure digest helpers ---------------------------------------------------


def test_render_stage_table_ranks_by_total_cost():
    s = _settings()
    stage_costs = {
        "implement": [1.0, 2.0, 3.0],  # $6 total
        "retrospect": [4.0, 4.0],  # $8 total
        "review": [0.1],  # $0.1
    }
    table = car._render_stage_table(stage_costs, s)
    # Retrospect ($8) ranks above implement ($6) above review.
    assert table.index("retrospect") < table.index("implement") < table.index("review")
    # Percentages present and total computed.
    assert "$14.1000" in table or "14.1" in table


def test_stage_tier_resolves_known_stage():
    s = _settings(audit_model="deepseek/deepseek-v4-flash", llm_backend="deepseek")
    tier = car._stage_tier(s, "audit")
    assert "flash" in tier and "cheap" in tier
    # Unknown stage degrades gracefully.
    assert "varies" in car._stage_tier(s, "ci_fix")


def test_stage_tier_backend_aware_on_claude_sdk():
    """On claude_sdk the digest reports the EFFECTIVE Claude model (the
    deepseek config name is ignored), keeping the correct tier class."""
    s = _settings(llm_backend="claude_sdk")
    # implement runs on `model` (deepseek-v4-pro = non-flash) → default → opus
    pro = car._stage_tier(s, "implement")
    assert "claude-opus" in pro and "capable/default" in pro
    s2 = _settings(audit_model="deepseek/deepseek-v4-flash", llm_backend="claude_sdk")
    cheap = car._stage_tier(s2, "audit")
    assert "claude-haiku" in cheap and "cheap" in cheap


def test_token_split_and_tool_calls():
    obs = [
        {"type": "GENERATION", "usage": {"input": 1000, "output": 100}},
        {"type": "GENERATION", "usage": {"promptTokens": 500, "completionTokens": 50}},
        {"type": "SPAN", "name": "read_file"},
        {"type": "SPAN", "name": "read_file"},
        {"type": "SPAN", "name": "explore"},
    ]
    split = car._token_split(obs)
    assert "input=1,500" in split and "output=150" in split
    tools = car._tool_calls(obs)
    assert "read_file×2" in tools and "explore×1" in tools


def test_observation_is_error():
    from robotsix_mill.langfuse import client as lf

    assert lf._observation_is_error({"level": "ERROR"})
    assert lf._observation_is_error({"output": "Error: boom"})
    assert lf._observation_is_error({"output": "non-zero exit status 1"})
    assert not lf._observation_is_error({"level": "DEFAULT", "output": "ok"})


# --- cross-repo aggregation ------------------------------------------------


def test_collect_traces_aggregates_across_repos(monkeypatch):
    """Traces from multiple repos merge into one stage/session view, and
    repos without langfuse keys are skipped."""
    s = _settings()

    repos = {
        "mill": _repo("mill"),
        "mail": _repo("mail"),
        "nokeys": _repo("nokeys", keyed=False),
    }
    monkeypatch.setattr(car, "get_repos_config", lambda: SimpleNamespace(repos=repos))

    traces_by_repo = {
        "mill": [
            {"name": "implement", "totalCost": 2.0, "sessionId": "t1"},
            {"name": "retrospect", "totalCost": 3.0, "sessionId": "t1"},
        ],
        "mail": [
            {"name": "implement", "totalCost": 1.0, "sessionId": "t2"},
        ],
        "nokeys": [{"name": "implement", "totalCost": 99.0, "sessionId": "t3"}],
    }

    def _fake_window(settings, window, *, max_pages, caller_name, repo_config):
        return traces_by_repo[repo_config.repo_id]

    monkeypatch.setattr(car.lf, "_fetch_traces_time_window", _fake_window)

    col = car._collect_traces(s)
    # implement summed across mill + mail (the keyless repo is skipped).
    assert sum(col.stage_costs["implement"]) == 3.0
    assert sum(col.stage_costs["retrospect"]) == 3.0
    assert "t3" not in col.sessions  # keyless repo skipped
    # session t1 = implement + retrospect = $5 over 2 traces.
    assert col.sessions["t1"]["cost"] == 5.0
    assert col.sessions["t1"]["count"] == 2


# --- draft filing + dedup --------------------------------------------------


class _FakeService:
    def __init__(self):
        self.created = []
        self._recent = []

    def recent_proposals_for(self, source, limit=100):
        return self._recent

    def create(self, *, title, description, source, origin_session):
        t = SimpleNamespace(id=f"id-{len(self.created)}", title=title)
        self.created.append(t)
        return t


def test_file_drafts_dedups_by_title(monkeypatch):
    s = _settings()
    svc = _FakeService()
    svc._recent = [SimpleNamespace(title="cost: downgrade retrospect tier")]
    monkeypatch.setattr(car, "TicketService", lambda settings, board_id: svc)

    result = car.CostReductionResult(
        draft_titles=[
            "cost: downgrade retrospect tier",  # dup of existing
            "cost: trim refine context",  # new
        ],
        draft_bodies=["body A", "body B"],
        gap_ids=["retro_tier", "refine_ctx"],
    )
    created = car._file_drafts(result, s, "sess-1", "robotsix-mill")
    assert len(created) == 1
    assert svc.created[0].title == "cost: trim refine context"
    # The gap-id marker is embedded in the body for traceability.
    # (create() received description with the marker)


# --- agent-failure resilience ----------------------------------------------


def test_agent_failure_returns_empty_result(monkeypatch):
    """run_cost_analyst_pass does not propagate an exception from the agent;
    it returns an empty result with the incoming memory unchanged."""
    s = _settings()

    monkeypatch.setattr(car, "Settings", lambda: s)
    monkeypatch.setattr(car, "_build_cost_digest", lambda settings: "<digest/>")
    monkeypatch.setattr(car, "load_memory", lambda path: "PRE-PASS LEDGER")
    monkeypatch.setattr(car, "_gather_recent_proposals", lambda settings, board_id: "")
    monkeypatch.setattr(car, "get_repos_config", lambda: SimpleNamespace(repos={}))
    monkeypatch.setattr(car, "persist_memory", lambda *a, **k: None)

    def _boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(car, "run_cost_analyst_agent", _boom)

    result = car.run_cost_analyst_pass("sess-1")
    assert result.drafts_created == []
    assert result.updated_memory == "PRE-PASS LEDGER"
