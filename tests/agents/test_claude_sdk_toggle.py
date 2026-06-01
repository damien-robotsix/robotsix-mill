"""Tests for the reversible DeepSeek↔Claude-SDK backend toggle in build_agent."""

from __future__ import annotations

import tempfile
from unittest.mock import MagicMock, patch

from robotsix_mill.agents import base
from robotsix_mill.config import Settings


def _settings(**kw) -> Settings:
    return Settings(data_dir=tempfile.mkdtemp(), **kw)


class TestUseClaudeSdk:
    def test_default_is_deepseek(self):
        s = _settings()
        assert base._use_claude_sdk(s, "refine") is False
        assert base._use_claude_sdk(s, None) is False

    def test_global_backend_routes_all(self):
        s = _settings(llm_backend="claude_sdk")
        assert base._use_claude_sdk(s, "refine") is True
        assert base._use_claude_sdk(s, None) is True

    def test_per_agent_optin(self):
        s = _settings(claude_sdk_agents=["auto-approve", "dedup"])
        assert base._use_claude_sdk(s, "auto-approve") is True
        assert base._use_claude_sdk(s, "dedup") is True
        assert base._use_claude_sdk(s, "refine") is False  # not listed → DeepSeek


class TestBuildAgentRouting:
    def _build_routed(self, settings: Settings, *, name: str, model_name: str):
        captured: dict = {}
        provider = MagicMock()

        def _build(**kw):
            captured.update(kw)
            return "CLAUDE_HANDLE"

        provider.build_agent.side_effect = _build
        with patch(
            "robotsix_llmio.claude_sdk.provider.ClaudeSDKProvider",
            return_value=provider,
        ):
            handle = base.build_agent(
                settings,
                system_prompt="sys",
                name=name,
                model_name=model_name,
                report_issue=False,
                reply_to_thread=False,
                close_thread=False,
                ask_user=False,
            )
        return handle, captured

    def test_routes_opted_in_agent_to_claude(self):
        from robotsix_llmio.core.provider import Tier

        # Fallback off so build_agent returns the bound handle directly (this
        # test asserts on its delegation, not the fallback wrapper).
        s = _settings(
            claude_sdk_agents=["auto-approve"], claude_fallback_to_deepseek=False
        )
        handle, cap = self._build_routed(
            s, name="auto-approve", model_name="deepseek/deepseek-v4-flash"
        )
        # The handle is wrapped by the global Claude-run concurrency bound; the
        # wrapper delegates to the underlying provider handle.
        assert handle._handle == "CLAUDE_HANDLE"
        assert cap["tier"] == Tier.CHEAP  # flash → cheap tier
        assert cap["system_prompt"] == "sys"
        assert cap["name"] == "auto-approve"

    def test_repo_dir_threaded_as_workspace_root(self, tmp_path):
        """build_agent forwards repo_dir as workspace_root so the SDK confines
        its built-in Write/Edit tools to the ticket clone (not the host /app)."""
        captured: dict = {}
        provider = MagicMock()

        def _build(**kw):
            captured.update(kw)
            return "CLAUDE_HANDLE"

        provider.build_agent.side_effect = _build
        s = _settings(llm_backend="claude_sdk")
        with patch(
            "robotsix_llmio.claude_sdk.provider.ClaudeSDKProvider",
            return_value=provider,
        ):
            base.build_agent(
                s,
                system_prompt="sys",
                name="implement",
                model_name="deepseek/deepseek-v4-pro",
                repo_dir=tmp_path,
                report_issue=False,
                reply_to_thread=False,
                close_thread=False,
                ask_user=False,
            )
        assert captured["workspace_root"] == tmp_path

    def test_no_repo_dir_leaves_workspace_root_none(self):
        """A board-less agent (no repo_dir) confines nothing — workspace_root
        is None, preserving prior behavior."""
        captured: dict = {}
        provider = MagicMock()

        def _build(**kw):
            captured.update(kw)
            return "CLAUDE_HANDLE"

        provider.build_agent.side_effect = _build
        s = _settings(llm_backend="claude_sdk")
        with patch(
            "robotsix_llmio.claude_sdk.provider.ClaudeSDKProvider",
            return_value=provider,
        ):
            base.build_agent(
                s,
                system_prompt="sys",
                name="refine",
                model_name="deepseek/deepseek-v4-pro",
                report_issue=False,
                reply_to_thread=False,
                close_thread=False,
                ask_user=False,
            )
        assert captured["workspace_root"] is None

    def test_pro_model_maps_to_default_tier(self):
        from robotsix_llmio.core.provider import Tier

        s = _settings(llm_backend="claude_sdk")
        _, cap = self._build_routed(
            s, name="refine", model_name="deepseek/deepseek-v4-pro"
        )
        assert cap["tier"] == Tier.DEFAULT  # pro → default tier

    def test_tier_alias_cheap_maps_to_cheap_tier(self):
        from robotsix_llmio.core.provider import Tier

        s = _settings(llm_backend="claude_sdk")
        _, cap = self._build_routed(s, name="refine", model_name="cheap")
        assert cap["tier"] == Tier.CHEAP  # `model: cheap` → CHEAP

    def test_tier_alias_default_and_normal_map_to_default_tier(self):
        from robotsix_llmio.core.provider import Tier

        s = _settings(llm_backend="claude_sdk")
        for alias in ("default", "normal", "DEFAULT"):
            _, cap = self._build_routed(s, name="refine", model_name=alias)
            assert cap["tier"] == Tier.DEFAULT, alias

    def test_default_backend_does_not_touch_claude_provider(self):
        """With the default DeepSeek backend, ClaudeSDKProvider is never
        imported/instantiated (so no claude_agent_sdk dependency on that path).
        """
        s = _settings()
        # Mock the whole DeepSeek construction chain so we don't need a real
        # key/network: the model, the provider, and pydantic_ai.Agent itself
        # (a mock model would otherwise trip pydantic-ai's model inference,
        # which demands OPENAI_API_KEY).
        with (
            patch("robotsix_llmio.claude_sdk.provider.ClaudeSDKProvider") as claude_cls,
            patch(
                "robotsix_mill.agents.openrouter_cost.CostInstrumentedOpenRouterModel"
            ),
            patch("pydantic_ai.providers.openrouter.OpenRouterProvider"),
            patch("pydantic_ai.Agent"),
            patch.object(
                base, "get_secrets", return_value=MagicMock(openrouter_api_key="k")
            ),
        ):
            base.build_agent(
                s,
                system_prompt="sys",
                name="refine",
                report_issue=False,
                reply_to_thread=False,
                close_thread=False,
                ask_user=False,
            )
        claude_cls.assert_not_called()

    def test_tier_alias_resolves_concrete_model_on_deepseek_path(self):
        """On the DeepSeek backend, `model: cheap`/`default` is resolved to the
        concrete model string passed to CostInstrumentedOpenRouterModel."""
        s = _settings()
        for alias, expected in (
            ("cheap", "deepseek/deepseek-v4-flash"),
            ("default", "deepseek/deepseek-v4-pro"),
        ):
            with (
                patch(
                    "robotsix_mill.agents.openrouter_cost.CostInstrumentedOpenRouterModel"
                ) as model_cls,
                patch("pydantic_ai.providers.openrouter.OpenRouterProvider"),
                patch("pydantic_ai.Agent"),
                patch.object(
                    base, "get_secrets", return_value=MagicMock(openrouter_api_key="k")
                ),
            ):
                base.build_agent(
                    s,
                    system_prompt="sys",
                    name="refine",
                    model_name=alias,
                    report_issue=False,
                    reply_to_thread=False,
                    close_thread=False,
                    ask_user=False,
                )
            assert model_cls.call_args.args[0] == expected, alias
