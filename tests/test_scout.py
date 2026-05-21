"""Tests for the scout agent and runner."""

import json

import pytest

from robotsix_mill.agents import scouting
from robotsix_mill.scout_runner import run_scout_pass, ScoutPassResult
from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State


def _make_settings(tmp_path, **overrides):
    """Create Settings with data_dir pointing to tmp_path."""
    overrides.setdefault("MILL_DATA_DIR", str(tmp_path / "data"))
    return Settings(**overrides)


# ── helpers for building mock OpenRouter data ───────────────────────────


def _make_endpoint(
    provider_name: str = "ProviderA",
    status: str = "active",
    uptime: float = 0.99,
    supports_tool_calls: bool = True,
    context_length: int = 128_000,
    prompt_price: float | None = None,
    completion_price: float | None = None,
    latency_p99_ms: float | None = None,
    throughput_p50_tps: float | None = None,
) -> scouting.EndpointInfo:
    """Build a single EndpointInfo with optional latency/throughput."""
    latency = None
    if latency_p99_ms is not None:
        latency = scouting.PercentileStats(
            p50=latency_p99_ms * 0.5,
            p75=latency_p99_ms * 0.75,
            p90=latency_p99_ms * 0.9,
            p99=latency_p99_ms,
        )
    throughput = None
    if throughput_p50_tps is not None:
        throughput = scouting.PercentileStats(
            p50=throughput_p50_tps,
            p75=throughput_p50_tps * 1.2,
            p90=throughput_p50_tps * 1.4,
            p99=throughput_p50_tps * 1.6,
        )
    return scouting.EndpointInfo(
        provider_name=provider_name,
        status=status,
        uptime_last_30m=uptime,
        supports_tool_calls=supports_tool_calls,
        context_length=context_length,
        prompt_price=prompt_price,
        completion_price=completion_price,
        latency_last_30m=latency,
        throughput_last_30m=throughput,
    )


def _model_info(
    model_id: str,
    prompt_price: float = 3.0,
    completion_price: float = 5.0,
    name: str = "",
    context_length: int = 128_000,
    endpoints: list | None = None,
) -> scouting.ModelInfo:
    if endpoints is None:
        endpoints = [
            _make_endpoint("ProviderA"),
            _make_endpoint("ProviderB", uptime=0.98),
        ]
    return scouting.ModelInfo(
        id=model_id,
        name=name or model_id,
        context_length=context_length,
        prompt_price=prompt_price,
        completion_price=completion_price,
        endpoints=endpoints,
    )


def _single_provider_endpoints(status: str = "active", uptime: float = 0.98) -> list:
    return [_make_endpoint("SoloProvider", status=status, uptime=uptime)]


def _no_tool_calls_endpoints() -> list:
    return [_make_endpoint("NoToolsProvider", supports_tool_calls=False)]


# ── Robust mock helpers for patching _fetch_models / _fetch_endpoints ──


def _patch_scout(monkeypatch, models: dict[str, scouting.ModelInfo]):
    """Monkeypatch _fetch_models and _fetch_endpoints to serve the given
    model dict.  Any model_id not in the dict gets a single-provider
    active endpoint so it won't accidentally outscore deliberately-
    constructed test candidates."""

    def mock_fetch_models(client, settings):
        return dict(models)

    def mock_fetch_endpoints(client, settings, model_id):
        info = models.get(model_id)
        if info is not None:
            return info.endpoints
        # Unknown model_id: single-provider fallback so test candidates
        # with multi-provider endpoints are clearly better.
        return [_make_endpoint("Fallback", uptime=0.98)]

    monkeypatch.setattr(scouting, "_fetch_models", mock_fetch_models)
    monkeypatch.setattr(scouting, "_fetch_endpoints", mock_fetch_endpoints)


# ── Agent (scouting.run_scout_agent) tests ────────────────────────────


def test_scout_result_model():
    """ScoutResult has expected fields."""
    result = scouting.ScoutResult(
        updated_memory="memory",
        draft_titles=["t1"],
        draft_bodies=["b1"],
    )
    assert result.updated_memory == "memory"
    assert len(result.draft_titles) == 1
    assert len(result.draft_bodies) == 1


def test_clearly_better_candidate_produces_draft(tmp_path, monkeypatch):
    """When a candidate scores materially higher than the configured
    model, exactly one draft is produced naming the role + candidate +
    the concrete MILL_*_MODEL diff."""
    # Set ALL model slots so only the one we care about is evaluated
    # with a controlled comparison.
    settings = _make_settings(
        tmp_path,
        MILL_MODEL="deepseek/deepseek-v4-pro",
        MILL_EXPLORE_MODEL="anthropic/claude-sonnet-4-5",
        MILL_WEB_RESEARCH_MODEL="anthropic/claude-sonnet-4-5",
        MILL_TEST_MODEL="anthropic/claude-sonnet-4-5",
        MILL_REFINE_MODEL="anthropic/claude-sonnet-4-5",
        MILL_RETROSPECT_MODEL="anthropic/claude-sonnet-4-5",
        MILL_AUDIT_MODEL="anthropic/claude-sonnet-4-5",
        MILL_AGENT_CHECK_MODEL="anthropic/claude-sonnet-4-5",
    )

    # Current model: deepseek-v4-pro — single provider, so lower score
    current = _model_info(
        "deepseek/deepseek-v4-pro",
        prompt_price=2.0, completion_price=8.0,
        endpoints=_single_provider_endpoints(),
    )
    # Candidate: claude sonnet — multi-provider, higher score
    candidate = _model_info(
        "anthropic/claude-sonnet-4-5",
        prompt_price=3.0, completion_price=15.0,
    )

    _patch_scout(monkeypatch, {
        current.id: current,
        candidate.id: candidate,
    })

    result = scouting.run_scout_agent(settings=settings, memory="")

    # At least one draft for MILL_MODEL (the coordinator role)
    model_drafts = [t for t in result.draft_titles if "MILL_MODEL" in t]
    assert len(model_drafts) == 1
    assert "anthropic/claude-sonnet-4-5" in model_drafts[0]
    # Concrete diff in body
    body = result.draft_bodies[result.draft_titles.index(model_drafts[0])]
    assert "MILL_MODEL=anthropic/claude-sonnet-4-5" in body


def test_single_provider_candidate_flagged_fragile(tmp_path, monkeypatch):
    """A single-provider candidate is flagged fragile in the draft body."""
    settings = _make_settings(
        tmp_path,
        MILL_EXPLORE_MODEL="deepseek/deepseek-v4-pro",
        # Set other roles to the same candidate so they don't trigger extra evaluations
        MILL_MODEL="meta-llama/llama-4-maverick",
        MILL_WEB_RESEARCH_MODEL="meta-llama/llama-4-maverick",
        MILL_TEST_MODEL="meta-llama/llama-4-maverick",
        MILL_REFINE_MODEL="meta-llama/llama-4-maverick",
        MILL_RETROSPECT_MODEL="meta-llama/llama-4-maverick",
        MILL_AUDIT_MODEL="meta-llama/llama-4-maverick",
        MILL_AGENT_CHECK_MODEL="meta-llama/llama-4-maverick",
    )

    # Current: deepseek-v4-pro single-provider (for explore role)
    current = _model_info(
        "deepseek/deepseek-v4-pro",
        endpoints=_single_provider_endpoints(),
    )
    # Candidate: better score but also single-provider
    candidate = _model_info(
        "meta-llama/llama-4-maverick",
        prompt_price=0.2, completion_price=0.6,
        endpoints=_single_provider_endpoints(),
    )

    _patch_scout(monkeypatch, {
        current.id: current,
        candidate.id: candidate,
    })

    result = scouting.run_scout_agent(settings=settings, memory="")

    explore_drafts = [t for t in result.draft_titles if "MILL_EXPLORE_MODEL" in t]
    if explore_drafts:
        body = result.draft_bodies[result.draft_titles.index(explore_drafts[0])]
        assert "Fragile" in body or "single provider" in body.lower()


def test_all_configured_models_optimal_produces_no_draft(tmp_path, monkeypatch):
    """When all configured models are the best option, NO draft is produced."""
    # Set ALL model slots to the same model (which is also a candidate)
    model_id = "anthropic/claude-sonnet-4-5"
    settings = _make_settings(
        tmp_path,
        MILL_MODEL=model_id,
        MILL_EXPLORE_MODEL=model_id,
        MILL_WEB_RESEARCH_MODEL=model_id,
        MILL_TEST_MODEL=model_id,
        MILL_REFINE_MODEL=model_id,
        MILL_RETROSPECT_MODEL=model_id,
        MILL_AUDIT_MODEL=model_id,
        MILL_AGENT_CHECK_MODEL=model_id,
    )

    current = _model_info(model_id)

    _patch_scout(monkeypatch, {current.id: current})

    result = scouting.run_scout_agent(settings=settings, memory="")
    assert len(result.draft_titles) == 0
    assert len(result.draft_bodies) == 0


def test_preview_model_regression_draft(tmp_path, monkeypatch):
    """A configured model that is a preview model produces a regression draft."""
    model_id = "anthropic/claude-sonnet-4-5"
    settings = _make_settings(
        tmp_path,
        MILL_MODEL="openai/gpt-4o-preview",
        MILL_EXPLORE_MODEL=model_id,
        MILL_WEB_RESEARCH_MODEL=model_id,
        MILL_TEST_MODEL=model_id,
        MILL_REFINE_MODEL=model_id,
        MILL_RETROSPECT_MODEL=model_id,
        MILL_AUDIT_MODEL=model_id,
        MILL_AGENT_CHECK_MODEL=model_id,
    )

    current = _model_info("openai/gpt-4o-preview")
    candidate = _model_info(model_id)

    _patch_scout(monkeypatch, {
        current.id: current,
        candidate.id: candidate,
    })

    result = scouting.run_scout_agent(settings=settings, memory="")

    model_drafts = [t for t in result.draft_titles if "MILL_MODEL" in t]
    assert len(model_drafts) == 1
    assert "Regression" in result.draft_bodies[result.draft_titles.index(model_drafts[0])]
    assert "preview" in result.draft_bodies[result.draft_titles.index(model_drafts[0])].lower()


def test_dropped_to_one_provider_regression_draft(tmp_path, monkeypatch):
    """A configured model that has only one provider produces a regression draft."""
    model_id = "anthropic/claude-sonnet-4-5"
    settings = _make_settings(
        tmp_path,
        MILL_MODEL="deepseek/deepseek-v4-pro",
        MILL_EXPLORE_MODEL=model_id,
        MILL_WEB_RESEARCH_MODEL=model_id,
        MILL_TEST_MODEL=model_id,
        MILL_REFINE_MODEL=model_id,
        MILL_RETROSPECT_MODEL=model_id,
        MILL_AUDIT_MODEL=model_id,
        MILL_AGENT_CHECK_MODEL=model_id,
    )

    current = _model_info(
        "deepseek/deepseek-v4-pro",
        endpoints=_single_provider_endpoints(),
    )
    candidate = _model_info(model_id)

    _patch_scout(monkeypatch, {
        current.id: current,
        candidate.id: candidate,
    })

    result = scouting.run_scout_agent(settings=settings, memory="")

    model_drafts = [t for t in result.draft_titles if "MILL_MODEL" in t]
    assert len(model_drafts) == 1
    assert "Regression" in result.draft_bodies[result.draft_titles.index(model_drafts[0])]


def test_dedup_already_proposed_not_reproposed(tmp_path, monkeypatch):
    """A recommendation already in scout memory is NOT re-proposed."""
    model_id = "anthropic/claude-sonnet-4-5"
    settings = _make_settings(
        tmp_path,
        MILL_MODEL="deepseek/deepseek-v4-pro",
        MILL_EXPLORE_MODEL=model_id,
        MILL_WEB_RESEARCH_MODEL=model_id,
        MILL_TEST_MODEL=model_id,
        MILL_REFINE_MODEL=model_id,
        MILL_RETROSPECT_MODEL=model_id,
        MILL_AUDIT_MODEL=model_id,
        MILL_AGENT_CHECK_MODEL=model_id,
    )

    current = _model_info(
        "deepseek/deepseek-v4-pro",
        endpoints=_single_provider_endpoints(),
    )
    candidate = _model_info(model_id)

    _patch_scout(monkeypatch, {
        current.id: current,
        candidate.id: candidate,
    })

    # First run: should produce a draft
    result1 = scouting.run_scout_agent(settings=settings, memory="")
    assert len(result1.draft_titles) >= 1

    # Second run with the updated memory: should NOT re-propose
    result2 = scouting.run_scout_agent(
        settings=settings, memory=result1.updated_memory
    )
    model_drafts = [t for t in result2.draft_titles if "MILL_MODEL" in t]
    assert len(model_drafts) == 0


def test_empty_memory_parsed_as_empty(tmp_path, monkeypatch):
    """Missing/empty memory is parsed as empty set — no crash."""
    model_id = "anthropic/claude-sonnet-4-5"
    settings = _make_settings(
        tmp_path,
        MILL_MODEL="deepseek/deepseek-v4-pro",
        MILL_EXPLORE_MODEL=model_id,
        MILL_WEB_RESEARCH_MODEL=model_id,
        MILL_TEST_MODEL=model_id,
        MILL_REFINE_MODEL=model_id,
        MILL_RETROSPECT_MODEL=model_id,
        MILL_AUDIT_MODEL=model_id,
        MILL_AGENT_CHECK_MODEL=model_id,
    )

    current = _model_info(
        "deepseek/deepseek-v4-pro",
        endpoints=_single_provider_endpoints(),
    )
    candidate = _model_info(model_id)

    _patch_scout(monkeypatch, {
        current.id: current,
        candidate.id: candidate,
    })

    # Empty string — should not crash
    result = scouting.run_scout_agent(settings=settings, memory="")
    assert isinstance(result.updated_memory, str)
    assert len(result.updated_memory) > 0  # Memory is built even from empty


def test_memory_updated_includes_new_proposals(tmp_path, monkeypatch):
    """The returned memory includes new proposals."""
    model_id = "anthropic/claude-sonnet-4-5"
    settings = _make_settings(
        tmp_path,
        MILL_MODEL="deepseek/deepseek-v4-pro",
        MILL_EXPLORE_MODEL=model_id,
        MILL_WEB_RESEARCH_MODEL=model_id,
        MILL_TEST_MODEL=model_id,
        MILL_REFINE_MODEL=model_id,
        MILL_RETROSPECT_MODEL=model_id,
        MILL_AUDIT_MODEL=model_id,
        MILL_AGENT_CHECK_MODEL=model_id,
    )

    current = _model_info(
        "deepseek/deepseek-v4-pro",
        endpoints=_single_provider_endpoints(),
    )
    candidate = _model_info(model_id)

    _patch_scout(monkeypatch, {
        current.id: current,
        candidate.id: candidate,
    })

    result = scouting.run_scout_agent(settings=settings, memory="")
    assert "anthropic/claude-sonnet-4-5" in result.updated_memory
    assert "MILL_MODEL" in result.updated_memory


# ── _fetch_endpoints response-shape robustness ───────────────────────


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload):
        self._payload = payload

    def get(self, *_args, **_kwargs):
        return _FakeResp(self._payload)


def test_fetch_endpoints_nested_dict_shape(tmp_path):
    """OpenRouter's actual /models/{id}/endpoints response wraps the
    list inside data["data"]["endpoints"]. The old code iterated
    data["data"] directly as if it were the list, which yielded the
    dict's KEYS (strings) and crashed with 'str object has no
    attribute get' on the next .get() call. Regression guard."""
    s = _make_settings(tmp_path)
    payload = {
        "data": {
            "id": "x/y",
            "name": "X / Y",
            "endpoints": [{
                "provider_name": "ProviderA",
                "status": "active",
                "uptime_last_30m": 0.99,
                "supports_tool_calls": True,
                "context_length": 200000,
                "pricing": {"prompt": "0.0001", "completion": "0.0002"},
                "latency_last_30m": {"p50": 0.5, "p95": 1.2},
                "throughput_last_30m": {"p50": 60.0, "p95": 80.0},
            }],
        }
    }
    eps = scouting._fetch_endpoints(_FakeClient(payload), s, "x/y")
    assert len(eps) == 1
    assert eps[0].provider_name == "ProviderA"
    assert eps[0].status == "active"
    assert eps[0].prompt_price == pytest.approx(0.0001)


def test_fetch_endpoints_legacy_list_shape(tmp_path):
    """Tolerate the legacy shape where data["data"] is the endpoints
    list directly — used to be the case in older OpenRouter responses;
    the defensive branch keeps it working if the API ever reverts."""
    s = _make_settings(tmp_path)
    payload = {
        "data": [{
            "provider_name": "ProviderLegacy",
            "status": "active",
            "context_length": 100000,
            "pricing": {"prompt": "0.001"},
        }]
    }
    eps = scouting._fetch_endpoints(_FakeClient(payload), s, "x/y")
    assert len(eps) == 1
    assert eps[0].provider_name == "ProviderLegacy"


def test_fetch_endpoints_malformed_entries_are_skipped(tmp_path):
    """Non-dict items inside the endpoints list must be skipped, not
    crash the run."""
    s = _make_settings(tmp_path)
    payload = {"data": {"endpoints": ["junk", None, {"provider_name": "ok"}]}}
    eps = scouting._fetch_endpoints(_FakeClient(payload), s, "x/y")
    assert [e.provider_name for e in eps] == ["ok"]


def test_fetch_endpoints_unexpected_shape_returns_empty(tmp_path):
    """A wholly unexpected top-level shape returns [] cleanly."""
    s = _make_settings(tmp_path)
    eps = scouting._fetch_endpoints(_FakeClient({"data": 42}), s, "x/y")
    assert eps == []


def test_fetch_endpoints_status_as_int_does_not_crash(tmp_path):
    """OpenRouter currently returns ``status`` as an int (observed: 0
    for healthy, -2 for degraded). Older code typed it as ``str``,
    which made pydantic reject the record entirely
    ('Input should be a valid string [type=string_type, input_value=0]').
    EndpointInfo accepts int|str now; assert both."""
    s = _make_settings(tmp_path)
    payload = {"data": {"endpoints": [
        {"provider_name": "DeepInfra", "status": 0},
        {"provider_name": "Novita", "status": -2},
        {"provider_name": "Legacy", "status": "active"},
    ]}}
    eps = scouting._fetch_endpoints(_FakeClient(payload), s, "x/y")
    assert [(e.provider_name, e.status) for e in eps] == [
        ("DeepInfra", 0), ("Novita", -2), ("Legacy", "active"),
    ]


def test_active_provider_count_int_zero_means_active():
    """active_provider_count must treat both legacy ``"active"`` and
    new integer ``0`` as healthy. Otherwise every model evaluated
    after the OpenRouter schema change reports zero active providers
    and the scout heuristics break."""
    mi = scouting.ModelInfo(id="x/y", endpoints=[
        scouting.EndpointInfo(provider_name="A", status=0),
        scouting.EndpointInfo(provider_name="B", status=-2),
        scouting.EndpointInfo(provider_name="C", status="active"),
        scouting.EndpointInfo(provider_name="D", status="disabled"),
    ])
    assert mi.active_provider_count == 2  # A (int 0) + C ("active")


# ── ModelInfo properties ──────────────────────────────────────────────


def test_model_info_provider_count():
    mi = _model_info("test/id")
    assert mi.provider_count == 2
    assert mi.active_provider_count == 2
    assert mi.has_tool_calls is True
    assert mi.is_preview is False


def test_model_info_preview_heuristic():
    mi = _model_info("openai/gpt-4o-preview")
    assert mi.is_preview is True


def test_model_info_no_tool_calls():
    mi = _model_info("some/model", endpoints=_no_tool_calls_endpoints())
    assert mi.has_tool_calls is False


def test_model_info_zero_providers():
    mi = _model_info("ghost/model", endpoints=[])
    assert mi.provider_count == 0
    assert mi.active_provider_count == 0


def test_model_info_latency_properties():
    """max_latency_p99_ms returns the worst-case p99 across endpoints."""
    mi = _model_info("slow/model", endpoints=[
        _make_endpoint("A", latency_p99_ms=3000),
        _make_endpoint("B", latency_p99_ms=12000),
    ])
    assert mi.max_latency_p99_ms == 12000


def test_model_info_latency_none_when_no_data():
    """When no endpoint reports latency, max_latency_p99_ms is None."""
    mi = _model_info("no-lat/model", endpoints=[
        _make_endpoint("A", latency_p99_ms=None),
    ])
    assert mi.max_latency_p99_ms is None


def test_model_info_throughput_properties():
    """min_throughput_p50_tps returns the worst-case p50 across endpoints."""
    mi = _model_info("slow-tput/model", endpoints=[
        _make_endpoint("A", throughput_p50_tps=50),
        _make_endpoint("B", throughput_p50_tps=15),
    ])
    assert mi.min_throughput_p50_tps == 15


def test_model_info_throughput_none_when_no_data():
    """When no endpoint reports throughput, min_throughput_p50_tps is None."""
    mi = _model_info("no-tput/model", endpoints=[
        _make_endpoint("A", throughput_p50_tps=None),
    ])
    assert mi.min_throughput_p50_tps is None


def test_model_info_estimated_slow_generation_seconds():
    """Estimated generation time uses worst-case p50 throughput."""
    mi = _model_info("slow-gen/model", endpoints=[
        _make_endpoint("A", throughput_p50_tps=10),  # 4000 / 10 = 400s
    ])
    assert mi.estimated_slow_generation_seconds == pytest.approx(400.0)


def test_model_info_estimated_slow_generation_none_without_throughput():
    """No throughput data → estimated generation time is None."""
    mi = _model_info("no-tput/model", endpoints=[
        _make_endpoint("A"),
    ])
    assert mi.estimated_slow_generation_seconds is None


def test_model_info_estimated_slow_generation_zero_throughput():
    """Zero throughput → estimated generation time is None (avoid div/0)."""
    mi = _model_info("zero-tput/model", endpoints=[
        _make_endpoint("A", throughput_p50_tps=0),
    ])
    assert mi.estimated_slow_generation_seconds is None


# ── Latency / throughput scoring ─────────────────────────────────────


def test_evaluate_slow_latency_flag():
    """P99 TTFT > 10s gets SLOW_LATENCY flag."""
    mi = _model_info("slow/model", endpoints=[
        _make_endpoint("A", latency_p99_ms=15_000),
    ])
    e = scouting._evaluate_model("slow/model", "c", "V", "capable", mi)
    assert "SLOW_LATENCY" in e.flags


def test_evaluate_slow_throughput_flag():
    """P50 throughput < 20 tps gets SLOW_THROUGHPUT flag."""
    mi = _model_info("slow-tput/model", endpoints=[
        _make_endpoint("A", throughput_p50_tps=10),
    ])
    e = scouting._evaluate_model("slow-tput/model", "c", "V", "capable", mi)
    assert "SLOW_THROUGHPUT" in e.flags


def test_evaluate_no_latency_flag_when_fast():
    """Fast model does not get latency/throughput flags."""
    mi = _model_info("fast/model", endpoints=[
        _make_endpoint("A", latency_p99_ms=500, throughput_p50_tps=80),
    ])
    e = scouting._evaluate_model("fast/model", "c", "V", "capable", mi)
    assert "SLOW_LATENCY" not in e.flags
    assert "SLOW_THROUGHPUT" not in e.flags


def test_evaluate_latency_reduces_score():
    """High-latency models score lower than low-latency models, all else equal."""
    slow = _model_info("slow/model", endpoints=[
        _make_endpoint("A", latency_p99_ms=20_000, throughput_p50_tps=50),
    ])
    fast = _model_info("fast/model", endpoints=[
        _make_endpoint("A", latency_p99_ms=500, throughput_p50_tps=50),
    ])
    eval_slow = scouting._evaluate_model("slow/model", "c", "V", "capable", slow)
    eval_fast = scouting._evaluate_model("fast/model", "c", "V", "capable", fast)
    assert eval_fast.score > eval_slow.score


def test_evaluate_low_throughput_reduces_score():
    """Low-throughput models score lower than high-throughput models."""
    slow = _model_info("slow-tput/model", endpoints=[
        _make_endpoint("A", latency_p99_ms=1000, throughput_p50_tps=5),
    ])
    fast = _model_info("fast-tput/model", endpoints=[
        _make_endpoint("A", latency_p99_ms=1000, throughput_p50_tps=100),
    ])
    eval_slow = scouting._evaluate_model("slow-tput/model", "c", "V", "capable", slow)
    eval_fast = scouting._evaluate_model("fast-tput/model", "c", "V", "capable", fast)
    assert eval_fast.score > eval_slow.score


# ── Evaluation scoring ────────────────────────────────────────────────


def test_evaluate_capable_role_prefers_multi_provider():
    """Multi-provider model scores higher than single-provider for capable roles."""
    multi = _model_info("multi/prov", endpoints=[
        _make_endpoint("A"),
        _make_endpoint("B"),
        _make_endpoint("C"),
    ])
    single = _model_info("single/prov", endpoints=_single_provider_endpoints())

    eval_multi = scouting._evaluate_model("multi/prov", "coordinator", "MILL_MODEL", "capable", multi)
    eval_single = scouting._evaluate_model("single/prov", "coordinator", "MILL_MODEL", "capable", single)

    assert eval_multi.score > eval_single.score
    assert "SINGLE_PROVIDER" in eval_single.flags


def test_evaluate_flags_no_tool_calls_for_capable():
    """Capable/structured roles flag models without tool calls."""
    mi = _model_info("no/tools", endpoints=_no_tool_calls_endpoints())
    e = scouting._evaluate_model("no/tools", "coordinator", "MILL_MODEL", "capable", mi)
    assert "NO_TOOL_CALLS" in e.flags


def test_evaluate_cheap_role_no_tool_call_flag():
    """Cheap roles do NOT flag missing tool calls."""
    mi = _model_info("no/tools", endpoints=_no_tool_calls_endpoints())
    e = scouting._evaluate_model("no/tools", "scout", "MILL_EXPLORE_MODEL", "cheap", mi)
    assert "NO_TOOL_CALLS" not in e.flags


def test_evaluate_preview_penalty():
    """Preview models get a score penalty."""
    preview = _model_info("openai/gpt-4o-preview")
    non_preview = _model_info("openai/gpt-4o", endpoints=[
        _make_endpoint("A"),
    ])
    eval_preview = scouting._evaluate_model("openai/gpt-4o-preview", "c", "V", "capable", preview)
    eval_non = scouting._evaluate_model("openai/gpt-4o", "c", "V", "capable", non_preview)
    # Both single-provider, but preview has extra penalty
    assert eval_non.score > eval_preview.score
    assert "PREVIEW" in eval_preview.flags


def test_evaluate_zero_providers():
    """Zero-provider model gets flagged."""
    mi = _model_info("ghost/model", endpoints=[])
    e = scouting._evaluate_model("ghost/model", "c", "V", "capable", mi)
    assert "ZERO_PROVIDERS" in e.flags


# ── Latency timeout advisory ──────────────────────────────────────────


def test_latency_timeout_note_slow_candidate(tmp_path, monkeypatch):
    """A slow candidate gets a timeout advisory in the draft body."""
    model_id = "anthropic/claude-sonnet-4-5"
    settings = _make_settings(
        tmp_path,
        MILL_MODEL="deepseek/deepseek-v4-pro",
        MILL_EXPLORE_MODEL=model_id,
        MILL_WEB_RESEARCH_MODEL=model_id,
        MILL_TEST_MODEL=model_id,
        MILL_REFINE_MODEL=model_id,
        MILL_RETROSPECT_MODEL=model_id,
        MILL_AUDIT_MODEL=model_id,
        MILL_AGENT_CHECK_MODEL=model_id,
        MILL_MODEL_REQUEST_TIMEOUT="120",
    )

    # Current: single provider, low score (but triggers regression draft)
    current = _model_info(
        "deepseek/deepseek-v4-pro",
        endpoints=_single_provider_endpoints(),
    )
    # Candidate: multi-provider but SLOW (5 tps → 800s for 4k tokens).
    # Other CAPABLE_CANDIDATES (openai/gpt-4o, google/gemini-2.5-pro)
    # must score lower so the slow candidate wins as "best".
    # Give them zero providers so they are eliminated.
    other_candidate = _model_info(
        "openai/gpt-4o",
        endpoints=[],
    )
    other_candidate2 = _model_info(
        "google/gemini-2.5-pro",
        endpoints=[],
    )
    candidate = _model_info(
        model_id,
        endpoints=[
            _make_endpoint("A", throughput_p50_tps=5, latency_p99_ms=15_000),
            _make_endpoint("B", throughput_p50_tps=5, latency_p99_ms=14_000),
        ],
    )

    _patch_scout(monkeypatch, {
        current.id: current,
        candidate.id: candidate,
        other_candidate.id: other_candidate,
        other_candidate2.id: other_candidate2,
    })

    result = scouting.run_scout_agent(settings=settings, memory="")
    model_drafts = [t for t in result.draft_titles if "MILL_MODEL" in t]
    assert len(model_drafts) >= 1
    body = result.draft_bodies[result.draft_titles.index(model_drafts[0])]
    # Should contain timeout advisory
    assert "MILL_MODEL_REQUEST_TIMEOUT" in body
    # "Latency / timeout advisory" section
    assert "Latency / timeout advisory" in body or "timeout" in body.lower()


def test_latency_timeout_note_fast_candidate_no_advisory(tmp_path, monkeypatch):
    """A fast candidate does NOT get a timeout advisory."""
    model_id = "openai/gpt-4o"
    settings = _make_settings(
        tmp_path,
        MILL_MODEL="deepseek/deepseek-v4-pro",
        MILL_EXPLORE_MODEL=model_id,
        MILL_WEB_RESEARCH_MODEL=model_id,
        MILL_TEST_MODEL=model_id,
        MILL_REFINE_MODEL=model_id,
        MILL_RETROSPECT_MODEL=model_id,
        MILL_AUDIT_MODEL=model_id,
        MILL_AGENT_CHECK_MODEL=model_id,
    )

    current = _model_info(
        "deepseek/deepseek-v4-pro",
        endpoints=_single_provider_endpoints(),
    )
    # Fast candidate
    candidate = _model_info(
        model_id,
        endpoints=[
            _make_endpoint("A", throughput_p50_tps=80, latency_p99_ms=2000),
            _make_endpoint("B", throughput_p50_tps=75, latency_p99_ms=2500),
        ],
    )

    _patch_scout(monkeypatch, {
        current.id: current,
        candidate.id: candidate,
    })

    result = scouting.run_scout_agent(settings=settings, memory="")
    model_drafts = [t for t in result.draft_titles if "MILL_MODEL" in t]
    if model_drafts:
        body = result.draft_bodies[result.draft_titles.index(model_drafts[0])]
        # Should NOT contain timeout advisory for a fast model
        assert "Latency / timeout advisory" not in body


def test_latency_timeout_note_function(tmp_path):
    """_latency_timeout_note returns expected advisory for slow models."""
    settings = _make_settings(tmp_path, MILL_MODEL_REQUEST_TIMEOUT="120")
    e = scouting.EvalResult(
        model_id="slow/model",
        role_name="coordinator",
        env_var="MILL_MODEL",
        max_latency_p99_ms=15_000,
        min_throughput_p50_tps=5,
        estimated_slow_generation_s=800.0,
    )
    note = scouting._latency_timeout_note(e, settings)
    assert note is not None
    assert "800s" in note
    assert "MILL_MODEL_REQUEST_TIMEOUT" in note


def test_latency_timeout_note_none_for_fast_model(tmp_path):
    """_latency_timeout_note returns None when model is fast enough."""
    settings = _make_settings(tmp_path)
    e = scouting.EvalResult(
        model_id="fast/model",
        role_name="coordinator",
        env_var="MILL_MODEL",
        max_latency_p99_ms=500,
        min_throughput_p50_tps=100,
        estimated_slow_generation_s=40.0,
    )
    note = scouting._latency_timeout_note(e, settings)
    assert note is None


def test_latency_timeout_note_warns_when_close_to_timeout(tmp_path):
    """When estimated gen is > 70% of timeout, warns about limited headroom."""
    settings = _make_settings(tmp_path, MILL_MODEL_REQUEST_TIMEOUT="120")
    e = scouting.EvalResult(
        model_id="borderline/model",
        role_name="coordinator",
        env_var="MILL_MODEL",
        max_latency_p99_ms=2000,
        min_throughput_p50_tps=40,
        estimated_slow_generation_s=100.0,  # 100/120 = 83%
    )
    note = scouting._latency_timeout_note(e, settings)
    assert note is not None
    assert "headroom" in note.lower() or "monitor" in note.lower()


# ── _fragile_text ─────────────────────────────────────────────────────


def test_fragile_text_single_provider():
    """_fragile_text includes single-provider warning."""
    e = scouting.EvalResult(
        model_id="m", role_name="r", env_var="V",
        flags=["SINGLE_PROVIDER"],
    )
    text = scouting._fragile_text(e)
    assert "single provider" in text.lower()
    assert "Fragile" in text


def test_fragile_text_preview():
    """_fragile_text includes preview warning."""
    e = scouting.EvalResult(
        model_id="m-preview", role_name="r", env_var="V",
        is_preview=True,
    )
    text = scouting._fragile_text(e)
    assert "preview" in text.lower()
    assert "Fragile" in text


def test_fragile_text_no_flags_empty():
    """_fragile_text returns empty string when no fragility flags."""
    e = scouting.EvalResult(
        model_id="m", role_name="r", env_var="V",
    )
    assert scouting._fragile_text(e) == ""


# ── _build_draft_body ─────────────────────────────────────────────────


def test_build_draft_body_regression(tmp_path):
    """_build_draft_body regression kind includes 'Regression detected' heading."""
    settings = _make_settings(tmp_path)
    best = scouting.EvalResult(
        model_id="best/model", role_name="c", env_var="MILL_MODEL",
        total_providers=2, active_providers=2, max_uptime=0.99,
        flags=["SINGLE_PROVIDER"],
        score=50,
    )
    current = scouting.EvalResult(
        model_id="cur/model", role_name="c", env_var="MILL_MODEL",
        total_providers=1, active_providers=1, max_uptime=0.98,
        flags=["SINGLE_PROVIDER"],
        score=30,
    )
    body = scouting._build_draft_body(
        reason_kind="regression",
        reason_text="Test regression reason.",
        env_var="MILL_MODEL",
        configured_id="cur/model",
        best=best,
        current_eval=current,
        label="coordinator",
        settings=settings,
    )
    assert "Regression detected" in body
    assert "MILL_MODEL=best/model" in body
    assert "Fragile" in body


def test_build_draft_body_improvement(tmp_path):
    """_build_draft_body improvement kind includes 'Improvement identified' heading."""
    settings = _make_settings(tmp_path)
    best = scouting.EvalResult(
        model_id="best/model", role_name="c", env_var="MILL_MODEL",
        total_providers=3, active_providers=3, max_uptime=0.99,
        score=70,
    )
    current = scouting.EvalResult(
        model_id="cur/model", role_name="c", env_var="MILL_MODEL",
        total_providers=2, active_providers=2, max_uptime=0.98,
        score=50,
    )
    body = scouting._build_draft_body(
        reason_kind="improvement",
        reason_text="Better model available.",
        env_var="MILL_MODEL",
        configured_id="cur/model",
        best=best,
        current_eval=current,
        label="coordinator",
        settings=settings,
    )
    assert "Improvement identified" in body
    assert "MILL_MODEL=best/model" in body


# ── PercentileStats ──────────────────────────────────────────────────


def test_percentile_stats_all_none_is_none():
    """_parse_percentile_stats returns None when all values are absent."""
    result = scouting._parse_percentile_stats({"p50": None, "p75": None, "p90": None, "p99": None})
    assert result is None


def test_percentile_stats_partial():
    """_parse_percentile_stats handles partial data."""
    result = scouting._parse_percentile_stats({"p50": 1000, "p75": None, "p90": None, "p99": 5000})
    assert result is not None
    assert result.p50 == 1000
    assert result.p75 is None
    assert result.p99 == 5000


# ── Memory parsing ────────────────────────────────────────────────────


def test_parse_memory_empty():
    result = scouting._parse_memory("")
    assert result == {}


def test_parse_memory_proposed_extracts_model_ids():
    memory = """# Scout Memory

## Proposed
- `anthropic/claude-sonnet-4-5` for MILL_MODEL (2025-01-15)
- `google/gemini-2.5-pro` for MILL_REFINE_MODEL (2025-01-15)

## Declined
- `openai/gpt-4o` for MILL_MODEL (2025-01-14)
"""
    result = scouting._parse_memory(memory)
    assert "MILL_MODEL" in result
    assert "anthropic/claude-sonnet-4-5" in result["MILL_MODEL"]
    assert "MILL_REFINE_MODEL" in result
    assert "google/gemini-2.5-pro" in result["MILL_REFINE_MODEL"]


def test_build_updated_memory_empty():
    result = scouting._build_updated_memory(
        "",
        [("MILL_MODEL", "anthropic/claude-sonnet-4-5", "coordinator")],
    )
    assert "# Scout Memory" in result
    assert "## Proposed" in result
    assert "anthropic/claude-sonnet-4-5" in result


def test_build_updated_memory_appends():
    old = "# Scout Memory\n\n## Proposed\n- `old/model` for MILL_MODEL (2025-01-01)\n\n## Declined\n"
    result = scouting._build_updated_memory(
        old,
        [("MILL_EXPLORE_MODEL", "new/model", "scout")],
    )
    assert "new/model" in result
    assert "old/model" in result  # preserved
    assert "MILL_EXPLORE_MODEL" in result


# ── Runner tests ──────────────────────────────────────────────────────


def test_run_scout_pass_empty_memory(tmp_path, monkeypatch):
    """With no memory file, runner passes empty string to agent."""
    settings = _make_settings(tmp_path)
    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return scouting.ScoutResult(
            updated_memory="new memory",
            draft_titles=[],
            draft_bodies=[],
        )

    monkeypatch.setattr(scouting, "run_scout_agent", mock_agent)
    monkeypatch.setattr("robotsix_mill.scout_runner.Settings", lambda: settings)

    run_scout_pass()
    assert captured_memory == [""]


def test_run_scout_pass_reads_existing_memory(tmp_path, monkeypatch):
    """Runner passes existing memory to agent."""
    settings = _make_settings(tmp_path)
    memory_file = settings.scout_memory_file
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text("# Existing memory\n## Proposed\n- `m/x` for MILL_MODEL (2025-01-01)\n", encoding="utf-8")

    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return scouting.ScoutResult(
            updated_memory="# Updated memory\n",
            draft_titles=[],
            draft_bodies=[],
        )

    monkeypatch.setattr(scouting, "run_scout_agent", mock_agent)
    monkeypatch.setattr("robotsix_mill.scout_runner.Settings", lambda: settings)

    run_scout_pass()
    assert captured_memory == ["# Existing memory\n## Proposed\n- `m/x` for MILL_MODEL (2025-01-01)\n"]


def test_run_scout_pass_writes_memory_verbatim(tmp_path, monkeypatch):
    """Runner writes agent's updated_memory verbatim."""
    settings = _make_settings(tmp_path)
    updated = "# Updated memory\n## Proposed\n- `m/x` for MILL_MODEL (2025-01-01)\n"

    def mock_agent(**kwargs):
        return scouting.ScoutResult(
            updated_memory=updated,
            draft_titles=[],
            draft_bodies=[],
        )

    monkeypatch.setattr(scouting, "run_scout_agent", mock_agent)
    monkeypatch.setattr("robotsix_mill.scout_runner.Settings", lambda: settings)

    run_scout_pass()
    memory_file = settings.scout_memory_file
    assert memory_file.exists()
    assert memory_file.read_text(encoding="utf-8") == updated


def test_run_scout_pass_creates_draft_tickets(tmp_path, monkeypatch):
    """Runner creates draft tickets for each proposal."""
    settings = _make_settings(tmp_path)
    db.init_db(settings)
    service = TicketService(settings)

    def mock_agent(**kwargs):
        return scouting.ScoutResult(
            updated_memory="# Memory\n",
            draft_titles=["Scout: switch model A", "Scout: switch model B"],
            draft_bodies=["BodyA", "BodyB"],
        )

    monkeypatch.setattr(scouting, "run_scout_agent", mock_agent)
    monkeypatch.setattr("robotsix_mill.scout_runner.Settings", lambda: settings)

    result = run_scout_pass()
    assert len(result.drafts_created) == 2
    # Verify tickets are in DB with source="scout"
    tickets = service.list()
    scout_tickets = [t for t in tickets if t.source == "scout"]
    assert len(scout_tickets) == 2
    assert scout_tickets[0].state == State.DRAFT


def test_run_scout_pass_no_drafts_when_empty(tmp_path, monkeypatch):
    """When agent returns no drafts, none are created."""
    settings = _make_settings(tmp_path)
    db.init_db(settings)

    def mock_agent(**kwargs):
        return scouting.ScoutResult(
            updated_memory="# Memory\n",
            draft_titles=[],
            draft_bodies=[],
        )

    monkeypatch.setattr(scouting, "run_scout_agent", mock_agent)
    monkeypatch.setattr("robotsix_mill.scout_runner.Settings", lambda: settings)

    result = run_scout_pass()
    assert len(result.drafts_created) == 0


def test_run_scout_pass_missing_memory_file(tmp_path, monkeypatch):
    """Missing memory file -> empty string passed, no error."""
    settings = _make_settings(tmp_path)
    memory_file = settings.scout_memory_file
    if memory_file.exists():
        memory_file.unlink()

    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return scouting.ScoutResult(
            updated_memory="# Memory\n",
            draft_titles=[],
            draft_bodies=[],
        )

    monkeypatch.setattr(scouting, "run_scout_agent", mock_agent)
    monkeypatch.setattr("robotsix_mill.scout_runner.Settings", lambda: settings)

    # Should not raise
    result = run_scout_pass()
    assert captured_memory == [""]


def test_scout_pass_result_structure(tmp_path, monkeypatch):
    """ScoutPassResult has correct structure."""
    settings = _make_settings(tmp_path)

    def mock_agent(**kwargs):
        return scouting.ScoutResult(
            updated_memory="mem",
            draft_titles=["t1"],
            draft_bodies=["b1"],
        )

    monkeypatch.setattr(scouting, "run_scout_agent", mock_agent)
    monkeypatch.setattr("robotsix_mill.scout_runner.Settings", lambda: settings)

    result = run_scout_pass()
    assert isinstance(result, ScoutPassResult)
    assert result.updated_memory == "mem"
    assert len(result.drafts_created) == 1


# ── Config tests ──────────────────────────────────────────────────────


def test_scout_config_defaults():
    """Scout config has correct defaults."""
    s = Settings()
    assert s.scout_periodic is False
    assert s.scout_interval_seconds == 86400
    assert s.scout_memory_path is None


def test_scout_memory_file_default(tmp_path):
    """When scout_memory_path is None, falls back to data_dir/scout_memory.md."""
    s = _make_settings(tmp_path)
    expected = s.data_dir / "scout_memory.md"
    assert s.scout_memory_file == expected


def test_scout_memory_file_override(tmp_path):
    """When scout_memory_path is set, uses that path."""
    custom_path = tmp_path / "custom_scout.md"
    s = _make_settings(tmp_path, MILL_SCOUT_MEMORY_PATH=str(custom_path))
    assert s.scout_memory_file == custom_path


# ── CLI tests ─────────────────────────────────────────────────────────


def test_scout_cli_command(capsys, tmp_path, monkeypatch):
    """Test that CLI scout command works."""
    from robotsix_mill.cli import main

    def mock_run(root=None):
        return ScoutPassResult(
            updated_memory="mem",
            drafts_created=[{"id": "123", "title": "Scout: switch model"}],
        )

    monkeypatch.setattr("robotsix_mill.scout_runner.run_scout_pass", mock_run)

    result = main(["scout"])
    assert result == 0
    captured = capsys.readouterr()
    assert "Scout pass complete" in captured.out


def test_scout_cli_json_output(capsys, tmp_path, monkeypatch):
    """Test JSON output flag."""
    from robotsix_mill.cli import main

    def mock_run(root=None):
        return ScoutPassResult(
            updated_memory="mem",
            drafts_created=[{"id": "123", "title": "Scout: switch model"}],
        )

    monkeypatch.setattr("robotsix_mill.scout_runner.run_scout_pass", mock_run)

    result = main(["scout", "--json"])
    assert result == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "memory" in data
    assert "tickets_created" in data


# ── Worker periodic scout ─────────────────────────────────────────────


def test_scout_periodic_false_no_timer_started(tmp_path, monkeypatch):
    """When MILL_SCOUT_PERIODIC=false (default), Worker.start() does NOT
    create a scout poll task."""
    import asyncio
    from robotsix_mill.stages import StageContext
    from robotsix_mill.runtime.worker import Worker

    settings = _make_settings(tmp_path)
    db.init_db(settings)
    service = TicketService(settings)
    ctx = StageContext(settings=settings, service=service)

    async def _run():
        w = Worker(ctx)
        # scout_periodic is False by default
        assert w._scout_task is None
        w.start()
        try:
            assert w._scout_task is None  # No scout task created
        finally:
            await w.stop()

    asyncio.run(_run())


def test_scout_periodic_true_creates_task(tmp_path, monkeypatch):
    """When MILL_SCOUT_PERIODIC=true, Worker.start() creates a scout poll task."""
    import asyncio
    from robotsix_mill.stages import StageContext
    from robotsix_mill.runtime.worker import Worker

    settings = _make_settings(tmp_path, MILL_SCOUT_PERIODIC="true")
    db.init_db(settings)
    service = TicketService(settings)
    ctx = StageContext(settings=settings, service=service)

    async def _run():
        w = Worker(ctx)
        assert w._scout_task is None
        w.start()
        try:
            assert w._scout_task is not None
        finally:
            await w.stop()

    asyncio.run(_run())
