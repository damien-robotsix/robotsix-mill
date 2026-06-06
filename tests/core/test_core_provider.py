"""Core provider base class — Tier enum, _is_transient default, and the
``build_agent`` / ``call_with_retry`` wiring that every concrete provider
inherits."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from robotsix_llmio.core import provider as provider_module
from robotsix_llmio.core import retry as retry_module
from robotsix_llmio.core.provider import LLMProvider, Tier


class _HTTPErr(Exception):
    """Cheap stand-in for a ``ModelHTTPError`` — only the ``status_code`` attr
    matters for transient classification."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _MockProvider(LLMProvider):
    """Bare-minimum concrete provider: implements ``new_model`` only and
    records what tier it was asked for."""

    def __init__(self, model: Any = None, http_client: Any = None) -> None:
        self.model_obj = model if model is not None else object()
        self.http_client_obj = http_client if http_client is not None else object()
        self.new_model_calls: list[Tier] = []

    def new_model(self, tier: Tier = Tier.DEFAULT) -> tuple[Any, Any]:
        self.new_model_calls.append(tier)
        return self.model_obj, self.http_client_obj


# --- Tier enum --------------------------------------------------------------


def test_tier_values():
    assert Tier.DEFAULT.value == "default"
    assert Tier.CHEAP.value == "cheap"


def test_tier_is_str_enum():
    # ``str, Enum`` mixin: instances are both str and Enum so they can be
    # compared with plain string literals.
    assert isinstance(Tier.DEFAULT, str)
    assert isinstance(Tier.CHEAP, str)
    assert Tier.DEFAULT == "default"
    assert Tier.CHEAP == "cheap"


def test_tier_members():
    assert {t.name for t in Tier} == {"DEFAULT", "CHEAP"}


def test_new_model_default_tier_is_default():
    """Without an explicit tier, ``new_model`` should default to
    :attr:`Tier.DEFAULT`."""
    p = _MockProvider()
    p.new_model()
    assert p.new_model_calls == [Tier.DEFAULT]


# --- _is_transient default delegates to retry.is_transient ------------------


def test_is_transient_default_delegates_to_retry_is_transient():
    p = _MockProvider()
    # 429/5xx → transient; 4xx/other → not transient. These are the
    # behaviours owned by ``retry.is_transient``; the base provider must
    # forward verbatim.
    assert p._is_transient(_HTTPErr(503)) is True
    assert p._is_transient(_HTTPErr(429)) is True
    assert p._is_transient(_HTTPErr(400)) is False
    assert p._is_transient(_HTTPErr(404)) is False
    assert p._is_transient(ValueError("boom")) is False


def test_is_transient_default_recognises_httpx_timeout():
    p = _MockProvider()
    assert p._is_transient(httpx.ReadTimeout("slow")) is True
    assert p._is_transient(httpx.ConnectError("refused")) is True


def test_is_transient_default_calls_retry_module(monkeypatch):
    """The default implementation must funnel through ``retry.is_transient``
    so provider layers can widen by overriding."""
    seen: list[BaseException] = []

    def fake(exc: BaseException) -> bool:
        seen.append(exc)
        return True

    monkeypatch.setattr(retry_module, "is_transient", fake)
    p = _MockProvider()
    err = ValueError("probe")
    assert p._is_transient(err) is True
    assert seen == [err]


def test_is_transient_override_is_used_by_call_with_retry():
    """A subclass that widens ``_is_transient`` causes ``call_with_retry`` to
    retry on the wider error set."""

    class _ValueErrProvider(_MockProvider):
        def _is_transient(self, exc: BaseException) -> bool:
            return isinstance(exc, ValueError)

    p = _ValueErrProvider()
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 2:
            raise ValueError("provider-specific transient")
        return "ok"

    out = p.call_with_retry(fn, sleep=lambda _d: None)
    assert out == "ok"
    assert calls["n"] == 2


# --- build_agent wiring -----------------------------------------------------


def test_build_agent_calls_new_model_with_tier(monkeypatch):
    p = _MockProvider()
    captured: dict[str, Any] = {}

    def fake_build_agent(model, http_client, **kwargs):
        captured["model"] = model
        captured["http_client"] = http_client
        captured.update(kwargs)
        return SimpleNamespace(_agent=model)

    monkeypatch.setattr(provider_module, "_build_agent", fake_build_agent)
    p.build_agent(tier=Tier.CHEAP, system_prompt="sys")
    assert p.new_model_calls == [Tier.CHEAP]
    assert captured["model"] is p.model_obj
    assert captured["http_client"] is p.http_client_obj


def test_build_agent_default_tier_is_default(monkeypatch):
    p = _MockProvider()

    def fake_build_agent(*_args, **_kwargs):
        return SimpleNamespace()

    monkeypatch.setattr(provider_module, "_build_agent", fake_build_agent)
    p.build_agent(system_prompt="sys")
    assert p.new_model_calls == [Tier.DEFAULT]


def test_build_agent_threads_kwargs(monkeypatch):
    p = _MockProvider()
    captured: dict[str, Any] = {}

    def fake_build_agent(model, http_client, **kwargs):
        captured["model"] = model
        captured["http_client"] = http_client
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(provider_module, "_build_agent", fake_build_agent)

    def _tool() -> None:  # pragma: no cover — never invoked
        return None

    tools = [_tool]
    p.build_agent(
        system_prompt="sys",
        tools=tools,
        output_type=dict,
        name="my-agent",
        retries=7,
    )
    assert captured["system_prompt"] == "sys"
    assert captured["tools"] is tools
    assert captured["output_type"] is dict
    assert captured["name"] == "my-agent"
    assert captured["retries"] == 7


def test_build_agent_returns_underlying_handle(monkeypatch):
    """``build_agent`` returns exactly what the inner ``_build_agent`` returns."""
    sentinel = SimpleNamespace(marker="handle")

    def fake_build_agent(*_args, **_kwargs):
        return sentinel

    monkeypatch.setattr(provider_module, "_build_agent", fake_build_agent)
    p = _MockProvider()
    assert p.build_agent(system_prompt="sys") is sentinel


# --- call_with_retry delegation --------------------------------------------


def test_call_with_retry_default_predicate_does_not_retry_valueerror():
    """With the default ``_is_transient``, a plain ``ValueError`` is fatal."""
    p = _MockProvider()
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise ValueError("not transient by default")

    with pytest.raises(ValueError):
        p.call_with_retry(fn, sleep=lambda _d: None)
    assert calls["n"] == 1


def test_call_with_retry_passes_through_to_retry_module(monkeypatch):
    """The base ``call_with_retry`` must hand its arguments to
    ``retry.call_with_retry`` and pin ``is_transient_fn`` to the provider's
    own ``_is_transient``."""
    p = _MockProvider()
    captured: dict[str, Any] = {}

    def fake_call_with_retry(fn, **kwargs):
        captured["fn"] = fn
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(retry_module, "call_with_retry", fake_call_with_retry)

    def target():  # pragma: no cover — never invoked
        return "x"

    def sleep(_d: float) -> None:  # pragma: no cover — never invoked
        return None

    def fallback():  # pragma: no cover — never invoked
        return "fb"

    out = p.call_with_retry(target, what="probe", sleep=sleep, fallback_fn=fallback)
    assert out == "ok"
    assert captured["fn"] is target
    assert captured["what"] == "probe"
    assert captured["sleep"] is sleep
    assert captured["fallback_fn"] is fallback
    # The predicate must be the provider's own bound ``_is_transient`` so
    # subclass overrides take effect.
    assert captured["is_transient_fn"] == p._is_transient


def test_call_with_retry_retries_on_transient_5xx():
    """End-to-end: the provider's default predicate accepts 5xx, so the base
    ``call_with_retry`` recovers transient HTTP errors transparently."""
    p = _MockProvider()
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _HTTPErr(503)
        return "ok"

    out = p.call_with_retry(fn, sleep=lambda _d: None)
    assert out == "ok"
    assert calls["n"] == 3
