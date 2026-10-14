"""Refine-stage orchestration: model/complexity routing — trivial-scope
routing, subscription-tier routing, findings-based downgrades, and
re-refine round-counter escalation.

Split out of ``test_orchestration.py``; module-level helpers and the
agent mock seams are imported from that parent module (which retains
them).
"""

import json

import pytest

from robotsix_mill.agents.refining import (
    RefineResult,
    TriageResult,
)
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.stages import StageContext

# A genuine (> 120 char) spec body so ``_spec_is_degenerate`` never trips.
from tests.stages.refine.test_orchestration import (
    _REAL_SPEC,
    _add_sendback_event,
    _apply_default_mocks,
    _run_agent,
    _ticket,
)


@pytest.fixture
def ctx_factory(tmp_path, fake_sandbox):
    from robotsix_mill.config import RepoConfig, Settings

    counter = [0]

    def make(**env):
        db.reset_engine()
        s = Settings(data_dir=str(tmp_path / f"data{counter[0]}"), **env)
        db.init_db(s, board_id="test-board")
        svc = TicketService(s, board_id="test-board")
        counter[0] += 1
        return StageContext(
            settings=s,
            service=svc,
            repo_config=RepoConfig(
                repo_id="test-repo",
                board_id="test-board",
                langfuse_project_name="test",
                langfuse_public_key="pk-test",
                langfuse_secret_key="sk-test",
            ),
        )

    yield make
    db.reset_engine()


# ===========================================================================
# Trivial-scope routing tests (AC: orchestration routing)
# ===========================================================================


def test_trivial_scope_routes_to_cheap_model(ctx_factory, monkeypatch, tmp_path):
    """When triage returns trivial_scope=True and the feature flag is on,
    run_refine_agent receives refine_level=s.refine_trivial_model_level."""
    ctx = ctx_factory(refine_trivial_routing_enabled=True)
    t = _ticket(ctx)

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="trivial one-liner",
            complexity="simple",
            trivial_scope=True,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert (
        refine_kwargs.get("refine_level") == ctx.settings.refine_trivial_model_level
    ), (
        f"Expected refine_level={ctx.settings.refine_trivial_model_level}, "
        f"got {refine_kwargs.get('refine_level')}"
    )
    # Cheap route: refining model is the subscription alias (sonnet).
    assert (
        refine_kwargs.get("refine_model")
        == ctx.settings.refine_trivial_subscription_model
    ), (
        f"Expected refine_model={ctx.settings.refine_trivial_subscription_model!r} "
        f"on cheap route, got {refine_kwargs.get('refine_model')!r}"
    )
    assert (
        refine_kwargs.get("request_limit_override")
        == ctx.settings.refine_request_limit_simple
    ), (
        f"Expected request_limit_override={ctx.settings.refine_request_limit_simple} "
        f"on cheap route, got {refine_kwargs.get('request_limit_override')!r}"
    )


def test_non_trivial_scope_routes_to_none(ctx_factory, monkeypatch, tmp_path):
    """When triage returns trivial_scope=False, refine_level is None (Opus default)."""
    ctx = ctx_factory(refine_trivial_routing_enabled=True)
    t = _ticket(ctx)

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="multi-file refactor",
            complexity="needs-exploration",
            trivial_scope=False,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert refine_kwargs.get("refine_level") is None, (
        f"Expected refine_level=None for non-trivial, "
        f"got {refine_kwargs.get('refine_level')}"
    )


def test_flag_off_forces_none_regardless_of_verdict(ctx_factory, monkeypatch, tmp_path):
    """When refine_trivial_routing_enabled=False, refine_level is always None
    even when triage returns trivial_scope=True."""
    ctx = ctx_factory(refine_trivial_routing_enabled=False)
    t = _ticket(ctx)

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="trivial but flag off",
            complexity="simple",
            trivial_scope=True,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert refine_kwargs.get("refine_level") is None, (
        f"Expected refine_level=None when flag is off, "
        f"got {refine_kwargs.get('refine_level')}"
    )


def test_missing_verdict_defaults_to_false(ctx_factory, monkeypatch, tmp_path):
    """When no triage artifact is written (e.g. reviewer sendback, triage
    disabled), _read_triage_trivial returns False, refine_level=None."""
    ctx = ctx_factory(refine_trivial_routing_enabled=True)
    t = _ticket(ctx)

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="normal refine",
            complexity="needs-exploration",
            trivial_scope=None,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert refine_kwargs.get("refine_level") is None, (
        f"Expected refine_level=None when trivial_scope is None, "
        f"got {refine_kwargs.get('refine_level')}"
    )


def test_trivial_scope_true_persisted_to_artifact(ctx_factory, monkeypatch, tmp_path):
    """When triage returns trivial_scope=True, the artifact file records it."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="trivial",
            complexity="simple",
            trivial_scope=True,
        ),
    )

    _run_agent(ctx, t, tmp_path)

    from robotsix_mill.stages.refine.orchestration import _read_triage_trivial

    ws = ctx.service.workspace(t)
    data = json.loads(
        (ws.artifacts_dir / "triage_complexity.json").read_text(encoding="utf-8")
    )
    assert data.get("trivial_scope") is True
    assert _read_triage_trivial(ws) is True


def test_triage_findings_forwarded_to_run_refine_agent(
    ctx_factory, monkeypatch, tmp_path
):
    """When triage returns exploration_findings, the value reaches
    run_refine_agent via the triage_findings keyword argument."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="multi-file",
            complexity="needs-exploration",
            exploration_findings="- Verified `src/foo.py` exists (342 lines)\n",
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert refine_kwargs.get("triage_findings") == (
        "- Verified `src/foo.py` exists (342 lines)\n"
    )


def test_triage_findings_none_not_forwarded_to_run_refine_agent(
    ctx_factory, monkeypatch, tmp_path
):
    """When triage does not populate exploration_findings (None),
    run_refine_agent receives triage_findings=None."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="simple change",
            complexity="simple",
            exploration_findings=None,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert refine_kwargs.get("triage_findings") is None


# ===========================================================================
# Findings-present downgrade: Opus -> cheaper Claude alias
# ===========================================================================


def test_findings_present_downgrades_opus_to_sonnet(ctx_factory, monkeypatch, tmp_path):
    """When complexity="needs-exploration" and triage produced substantial
    exploration findings, the refine_model is downgraded from Opus to the
    findings model alias (default "sonnet")."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="multi-file refactor",
            complexity="needs-exploration",
            trivial_scope=False,
            exploration_findings="x" * 300,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert refine_kwargs.get("refine_model") == (
        ctx.settings.refine_subscription_model_findings
    ), (
        f"Expected refine_model={ctx.settings.refine_subscription_model_findings!r}, "
        f"got {refine_kwargs.get('refine_model')!r}"
    )
    assert refine_kwargs.get("refine_level") is None, (
        "refine_level must be None (still level 3 / Claude-SDK)"
    )


def test_short_findings_keeps_opus(ctx_factory, monkeypatch, tmp_path):
    """When exploration_findings is below refine_findings_downgrade_min_chars,
    the Opus model is kept."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="needs exploration",
            complexity="needs-exploration",
            trivial_scope=False,
            exploration_findings="too short",
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert refine_kwargs.get("refine_model") == (
        ctx.settings.refine_subscription_model_complex
    ), (
        f"Expected refine_model={ctx.settings.refine_subscription_model_complex!r}, "
        f"got {refine_kwargs.get('refine_model')!r}"
    )


def test_no_findings_keeps_opus(ctx_factory, monkeypatch, tmp_path):
    """When exploration_findings is None (absent or unparseable artifact),
    the Opus model is kept."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="needs exploration",
            complexity="needs-exploration",
            trivial_scope=False,
            exploration_findings=None,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert refine_kwargs.get("refine_model") == (
        ctx.settings.refine_subscription_model_complex
    ), (
        f"Expected refine_model={ctx.settings.refine_subscription_model_complex!r}, "
        f"got {refine_kwargs.get('refine_model')!r}"
    )


def test_findings_downgrade_flag_off_keeps_opus(ctx_factory, monkeypatch, tmp_path):
    """When refine_findings_downgrade_enabled=False, substantial findings
    do NOT trigger the downgrade -- Opus is kept (prior behaviour)."""
    ctx = ctx_factory(refine_findings_downgrade_enabled=False)
    t = _ticket(ctx)

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="needs exploration",
            complexity="needs-exploration",
            trivial_scope=False,
            exploration_findings="x" * 300,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert refine_kwargs.get("refine_model") == (
        ctx.settings.refine_subscription_model_complex
    ), (
        f"Expected refine_model={ctx.settings.refine_subscription_model_complex!r} "
        f"(flag off), got {refine_kwargs.get('refine_model')!r}"
    )


def test_simple_complexity_unaffected_by_findings(ctx_factory, monkeypatch, tmp_path):
    """Regression: complexity="simple" is unaffected by the findings
    downgrade -- the elif chain keeps the simple path intact."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="simple fix",
            complexity="simple",
            trivial_scope=False,
            exploration_findings="x" * 300,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert refine_kwargs.get("refine_model") == (
        ctx.settings.refine_subscription_model_default
    ), (
        f"Expected refine_model={ctx.settings.refine_subscription_model_default!r} "
        f"(simple path), got {refine_kwargs.get('refine_model')!r}"
    )
    assert refine_kwargs.get("request_limit_override") == (
        ctx.settings.refine_request_limit_simple
    ), "request_limit_override must be set on the simple path"


# ===========================================================================
# Re-refine round counter → force cheap model after threshold
# ===========================================================================


def test_re_refine_counter_forces_cheap_after_threshold(
    ctx_factory, monkeypatch, tmp_path
):
    """A ticket with ≥ max_re_refine_cycles_before_cheap sendback events
    and a non-trivial triage verdict routes to the cheap model."""
    ctx = ctx_factory(max_re_refine_cycles_before_cheap=2)
    t = _ticket(ctx)

    # Simulate 2 prior "changes requested" sendbacks — at threshold.
    _add_sendback_event(ctx, t, "first round feedback")
    _add_sendback_event(ctx, t, "second round feedback")

    # Add an open reviewer comment so the sendback path activates.
    ctx.service.add_comment(t.id, "Please revise the scope.", author="user")

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    # Triage returns non-trivial so the trivial-routing block leaves
    # refine_level=None — the counter must force the downgrade.
    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="needs refinement",
            complexity="needs-exploration",
            trivial_scope=False,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert (
        refine_kwargs.get("refine_level") == ctx.settings.refine_trivial_model_level
    ), (
        f"Expected refine_level={ctx.settings.refine_trivial_model_level} "
        f"(cheap) after {ctx.settings.max_re_refine_cycles_before_cheap} "
        f"sendbacks, got {refine_kwargs.get('refine_level')}"
    )
    # Forced-cheap route: refining model is the subscription alias.
    assert (
        refine_kwargs.get("refine_model")
        == ctx.settings.refine_trivial_subscription_model
    ), (
        f"Expected refine_model={ctx.settings.refine_trivial_subscription_model!r} "
        f"on forced-cheap route, got {refine_kwargs.get('refine_model')!r}"
    )
    assert refine_kwargs.get("request_limit_override") == max(
        int(
            ctx.settings.refine_request_limit_simple
            * ctx.settings.refine_dynamic_limit_multiplier
        ),
        ctx.settings.refine_dynamic_limit_min,
    ), (
        f"Expected dynamic request_limit_override for forced-cheap route, "
        f"got {refine_kwargs.get('request_limit_override')!r}"
    )
    # Exploration sub-agents must be disabled on sendback.
    assert refine_kwargs.get("include_explore") is False
    assert refine_kwargs.get("include_parallel_explore") is False


def test_re_refine_below_threshold_keeps_opus(ctx_factory, monkeypatch, tmp_path):
    """A ticket with fewer than max_re_refine_cycles_before_cheap sendbacks
    and a non-trivial verdict keeps refine_level=None (full Opus)."""
    ctx = ctx_factory(max_re_refine_cycles_before_cheap=2)
    t = _ticket(ctx)

    # Only 1 prior sendback — below the default threshold of 2.
    _add_sendback_event(ctx, t, "one round of feedback")

    ctx.service.add_comment(t.id, "Please adjust.", author="user")

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="needs refinement",
            complexity="needs-exploration",
            trivial_scope=False,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert refine_kwargs.get("refine_level") is None, (
        f"Expected refine_level=None (full Opus) when below threshold, "
        f"got {refine_kwargs.get('refine_level')}"
    )


def test_re_refine_first_run_trivial_stays_cheap(ctx_factory, monkeypatch, tmp_path):
    """Regression: a re-refine where triage_complexity.json from the
    first round has trivial_scope=true stays on the cheap model
    regardless of the re-refine counter."""
    ctx = ctx_factory(
        max_re_refine_cycles_before_cheap=2,
        refine_trivial_routing_enabled=True,
    )
    t = _ticket(ctx)

    # Write the first-run triage artifact (simulating a prior round).
    ws = ctx.service.workspace(t)
    import json as _json

    (ws.artifacts_dir / "triage_complexity.json").write_text(
        _json.dumps({"complexity": "simple", "trivial_scope": True}),
        encoding="utf-8",
    )

    # Simulate 2 prior sendbacks (at threshold) — but the persisted
    # trivial verdict should keep the cheap model regardless.
    _add_sendback_event(ctx, t, "feedback 1")
    _add_sendback_event(ctx, t, "feedback 2")

    ctx.service.add_comment(t.id, "Revise.", author="user")

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    # Triage is skipped (reviewer_comments present), so the triage mock
    # is irrelevant — the trivial-routing block reads from the artifact.
    _apply_default_mocks(monkeypatch, run_refine_agent=_run)

    _run_agent(ctx, t, tmp_path)

    assert (
        refine_kwargs.get("refine_level") == ctx.settings.refine_trivial_model_level
    ), (
        f"Expected refine_level={ctx.settings.refine_trivial_model_level} "
        f"(cheap) from persisted first-run trivial verdict, "
        f"got {refine_kwargs.get('refine_level')}"
    )
    # Persisted trivial verdict → cheap route with subscription alias.
    assert (
        refine_kwargs.get("refine_model")
        == ctx.settings.refine_trivial_subscription_model
    ), (
        f"Expected refine_model={ctx.settings.refine_trivial_subscription_model!r} "
        f"from persisted trivial verdict, got {refine_kwargs.get('refine_model')!r}"
    )
    assert (
        refine_kwargs.get("request_limit_override")
        == ctx.settings.refine_request_limit_simple
    ), (
        f"Expected request_limit_override={ctx.settings.refine_request_limit_simple} "
        f"from persisted trivial verdict, got {refine_kwargs.get('request_limit_override')!r}"
    )


def test_re_refine_counter_disabled_by_zero(ctx_factory, monkeypatch, tmp_path):
    """When max_re_refine_cycles_before_cheap=0, the counter-forced
    downgrade is disabled — even with many sendbacks, refine_level
    stays None (full Opus) for non-trivial tickets."""
    ctx = ctx_factory(max_re_refine_cycles_before_cheap=0)
    t = _ticket(ctx)

    # Many sendbacks, but threshold is 0 (disabled).
    for i in range(5):
        _add_sendback_event(ctx, t, f"feedback {i}")

    ctx.service.add_comment(t.id, "Revise again.", author="user")

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="needs refinement",
            complexity="needs-exploration",
            trivial_scope=False,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert refine_kwargs.get("refine_level") is None, (
        f"Expected refine_level=None when counter is disabled (threshold=0), "
        f"got {refine_kwargs.get('refine_level')}"
    )


# ===========================================================================
# Complexity-gated Claude alias routing (subscription tier routing)
# ===========================================================================


def test_subscription_tier_routing_simple_uses_sonnet(
    ctx_factory, monkeypatch, tmp_path
):
    """With refine_subscription_tier_routing_enabled=True and
    complexity='simple', run_refine_agent receives refine_model='sonnet'
    and request_limit_override=40."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="simple one-liner",
            complexity="simple",
            trivial_scope=False,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert refine_kwargs.get("refine_model") == "sonnet", (
        f"Expected refine_model='sonnet' for simple complexity, "
        f"got {refine_kwargs.get('refine_model')!r}"
    )
    assert refine_kwargs.get("refine_level") is None, (
        "Expected no refine_level downgrade for non-trivial ticket"
    )
    assert refine_kwargs.get("request_limit_override") == 40, (
        f"Expected request_limit_override=40 for simple path, "
        f"got {refine_kwargs.get('request_limit_override')!r}"
    )


def test_subscription_tier_routing_needs_exploration_uses_opus(
    ctx_factory, monkeypatch, tmp_path
):
    """With refine_subscription_tier_routing_enabled=True and
    complexity='needs-exploration', run_refine_agent receives
    refine_model='opus' and no request_limit_override."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="multi-file change needs deep exploration",
            complexity="needs-exploration",
            trivial_scope=False,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert refine_kwargs.get("refine_model") == "opus", (
        f"Expected refine_model='opus' for needs-exploration, "
        f"got {refine_kwargs.get('refine_model')!r}"
    )
    assert refine_kwargs.get("refine_level") is None, (
        "Expected no refine_level downgrade for non-trivial ticket"
    )
    # Dynamic limit fires for non-simple complexity: base=80, ×1.5 → 120.
    assert refine_kwargs.get("request_limit_override") == max(
        int(
            ctx.settings.refine_request_limit
            * ctx.settings.refine_dynamic_limit_multiplier
        ),
        ctx.settings.refine_dynamic_limit_min,
    ), "Expected dynamic request_limit_override for needs-exploration path"


def test_subscription_tier_routing_disabled_no_alias(
    ctx_factory, monkeypatch, tmp_path
):
    """With refine_subscription_tier_routing_enabled=False,
    run_refine_agent receives refine_model=None (Opus-always,
    today's behaviour)."""
    ctx = ctx_factory()
    # Disable the feature flag.
    ctx.settings.refine_subscription_tier_routing_enabled = False
    t = _ticket(ctx)

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="simple change",
            complexity="simple",
            trivial_scope=False,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    assert refine_kwargs.get("refine_model") is None, (
        f"Expected refine_model=None when feature flag is off, "
        f"got {refine_kwargs.get('refine_model')!r}"
    )
    assert refine_kwargs.get("request_limit_override") is None, (
        "Expected no request_limit_override when feature flag is off"
    )


def test_trivial_scope_unchanged_by_tier_routing(ctx_factory, monkeypatch, tmp_path):
    """trivial_scope=True routes to refines via the cheap route (level 3
    subscription with sonnet), bypassing the subscription-tier complexity
    ladder — the cheap route is independent of tier routing."""
    ctx = ctx_factory()
    t = _ticket(ctx)

    refine_kwargs: dict = {}

    def _run(**kw):
        refine_kwargs.update(kw)
        return RefineResult(spec_markdown=_REAL_SPEC)

    _apply_default_mocks(
        monkeypatch,
        triage_refine=lambda **kw: TriageResult(
            decision="REFINE",
            reason="single-line mechanical change",
            complexity="simple",
            trivial_scope=True,
        ),
        run_refine_agent=_run,
    )

    _run_agent(ctx, t, tmp_path)

    # Trivial → level 3 (Claude subscription)
    assert (
        refine_kwargs.get("refine_level") == ctx.settings.refine_trivial_model_level
    ), (
        f"Expected refine_level={ctx.settings.refine_trivial_model_level} "
        f"for trivial ticket, "
        f"got {refine_kwargs.get('refine_level')!r}"
    )
    # Cheap route → sonnet on the subscription, not the tier-routing ladder.
    assert (
        refine_kwargs.get("refine_model")
        == ctx.settings.refine_trivial_subscription_model
    ), (
        f"Expected refine_model={ctx.settings.refine_trivial_subscription_model!r} "
        f"for trivial (subscription cheap route), "
        f"got {refine_kwargs.get('refine_model')!r}"
    )
    assert (
        refine_kwargs.get("request_limit_override")
        == ctx.settings.refine_request_limit_simple
    ), (
        f"Expected request_limit_override={ctx.settings.refine_request_limit_simple} "
        f"for trivial cheap route, "
        f"got {refine_kwargs.get('request_limit_override')!r}"
    )
