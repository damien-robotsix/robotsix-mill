"""The strong 'deep' authoring sub-agent (deep_refine / deep_implement)."""

from robotsix_mill.agents import deep
from robotsix_mill.agents.deep import (
    make_deep_implement_tool,
    make_deep_refine_tool,
)
from robotsix_mill.config import Settings


def _settings(tmp_path, **env):
    env.setdefault("MILL_DATA_DIR", str(tmp_path))
    return Settings(**env)


def test_no_key_degrades_not_raises(tmp_path):
    s = _settings(tmp_path, OPENROUTER_API_KEY="")
    for fn in (deep.run_deep_refine, deep.run_deep_implement):
        out = fn(settings=s, context="anything")
        assert "unavailable" in out and "OPENROUTER_API_KEY" in out


def test_refine_tool_delegates_to_seam(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    seen = {}

    def fake(*, settings, context):
        seen["ctx"] = context
        return "SPEC"

    monkeypatch.setattr(deep, "run_deep_refine", fake)
    assert make_deep_refine_tool(s)("title+draft") == "SPEC"
    assert seen["ctx"] == "title+draft"


def test_implement_tool_delegates_to_seam(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(
        deep, "run_deep_implement",
        lambda *, settings, context: f"PATCH<{context}>",
    )
    assert make_deep_implement_tool(s)("full ctx") == "PATCH<full ctx>"


def test_deep_uses_deep_model_bounded_no_online(tmp_path, monkeypatch):
    s = _settings(
        tmp_path,
        OPENROUTER_API_KEY="k",
        MILL_DEEP_MODEL="strong/v4",
        MILL_DEEP_MODEL_REQUEST_LIMIT="3",
    )
    cap = {}

    class FakeModel:
        def __init__(self, name, **kw):
            cap["model"] = name

    class FakeAgent:
        def __init__(self, **kw):
            cap["has_tools"] = bool(kw.get("tools"))

        def run_sync(self, ctx, *, usage_limits=None):
            cap["limit"] = usage_limits.request_limit
            return type("R", (), {"output": "  done  "})()

    import pydantic_ai
    import pydantic_ai.providers.openrouter as orp
    from robotsix_mill.agents import openrouter_cost as oc

    monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
    monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())
    monkeypatch.setattr(oc, "CostInstrumentedOpenRouterModel", FakeModel)

    out = deep.run_deep_implement(settings=s, context="c")
    assert out == "done"  # stripped
    assert cap["model"] == "strong/v4"  # exact, NO ":online"
    assert ":online" not in cap["model"]
    assert cap["limit"] == 3
    assert cap["has_tools"] is False  # the deep agent has no tools


def test_retrospect_uses_prompted_output_on_cheap_driver(
    tmp_path, monkeypatch
):
    """Regression: retrospect's structured output must be requested via
    PromptedOutput (not the default ToolOutput, which 404s the cheap
    driver) AND stay on the cheap driver model — NOT routed to the
    expensive deep_model."""
    from pydantic_ai import PromptedOutput

    from robotsix_mill.agents import base, retrospecting
    from robotsix_mill.agents.retrospecting import RetrospectResult

    s = _settings(
        tmp_path, OPENROUTER_API_KEY="k",
        MILL_MODEL="cheap/drv", MILL_DEEP_MODEL="strong/v4",
    )
    cap = {}

    def fake_build_agent(settings, *, system_prompt, output_type, **kw):
        cap["output_type"] = output_type
        cap["driver_model"] = settings.model

        class A:
            def run_sync(self, _p):
                return type("R", (), {
                    "output": RetrospectResult(findings="f", conclusion="c")
                })()

        return A()

    monkeypatch.setattr(base, "build_agent", fake_build_agent)

    out = retrospecting.run_retrospect_agent(
        settings=s, ticket_summary="t", history_text="h",
        langfuse_summary=None, memory="",
    )
    assert out.findings == "f"
    assert isinstance(cap["output_type"], PromptedOutput)  # not ToolOutput
    assert cap["driver_model"] == "cheap/drv"  # cheap, NOT strong/v4
