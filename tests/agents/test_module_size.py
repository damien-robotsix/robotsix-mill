"""Tests for the module_size agent and its dynamic-kwargs builder."""

from robotsix_mill.agents import module_size
from robotsix_mill.config import Settings
from robotsix_mill.core import db


def _make_settings(tmp_path, **overrides):
    """Create Settings with data_dir pointing to tmp_path."""
    overrides.setdefault("data_dir", str(tmp_path / "data"))
    s = Settings(**overrides)
    db.reset_engine()
    db.init_db(s, board_id="test-board")
    return s


# --- Module-level constants ---


def test_module_size_system_prompt_is_non_empty_string():
    """SYSTEM_PROMPT re-exports the YAML system_prompt without env-var resolution."""
    assert isinstance(module_size.SYSTEM_PROMPT, str)
    assert len(module_size.SYSTEM_PROMPT) > 0
    assert "module" in module_size.SYSTEM_PROMPT.lower()


def test_module_size_max_gaps():
    """MAX_GAPS is the expected constant."""
    assert module_size.MAX_GAPS == 3


# --- Runner registration ---


def test_run_module_size_agent_wires_runner(monkeypatch):
    """run_module_size_agent delegates to periodic_base.run_periodic_agent with
    the expected flags and definition_name."""
    captured: dict = {}

    def fake_run_periodic(**kwargs):
        captured["kwargs"] = kwargs
        return module_size.ModuleSizeResult()

    from robotsix_mill.agents import periodic_base

    monkeypatch.setattr(periodic_base, "run_periodic_agent", fake_run_periodic)

    module_size.run_module_size_agent(settings=Settings())

    kw = captured["kwargs"]
    assert kw["definition_name"] == "module_size"
    assert kw["max_gaps"] == 3
    assert kw["include_forge_url"] is True
    assert kw["include_run_command"] is True
    assert kw["include_parallel_commands"] is True


# --- _module_size_dynamic_kwargs ---


def test_module_size_dynamic_kwargs_defaults(tmp_path):
    """Default Settings produce the expected usage_limits and max_errors."""
    settings = _make_settings(tmp_path)
    kwargs = module_size._module_size_dynamic_kwargs(settings)

    limits = kwargs["usage_limits"]
    assert limits.request_limit == 60
    assert limits.tool_calls_limit == 80
    assert kwargs["max_errors"] == 20


def test_module_size_dynamic_kwargs_non_default(tmp_path):
    """Non-default Settings propagate every field."""
    settings = _make_settings(
        tmp_path,
        module_size_request_limit=10,
        module_size_max_tool_calls=15,
        module_size_max_errors=5,
    )
    kwargs = module_size._module_size_dynamic_kwargs(settings)

    limits = kwargs["usage_limits"]
    assert limits.request_limit == 10
    assert limits.tool_calls_limit == 15
    assert kwargs["max_errors"] == 5


def test_module_size_dynamic_kwargs_returns_usage_limits_instance(tmp_path):
    """_module_size_dynamic_kwargs returns a UsageLimits instance (not a dict)."""
    settings = _make_settings(tmp_path)
    kwargs = module_size._module_size_dynamic_kwargs(settings)

    from pydantic_ai.usage import UsageLimits

    assert isinstance(kwargs["usage_limits"], UsageLimits)
