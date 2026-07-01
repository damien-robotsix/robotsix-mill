"""Tests for the component-agent responder and config contract."""

from __future__ import annotations

import datetime
import logging
from unittest.mock import MagicMock, patch

import pytest

from robotsix_agent_comm.protocol import ConfigContractError

from robotsix_mill.component_agent.config_contract import (
    SETTABLE_KEYS,
    _is_secret_key,
    apply_config_update,
    describe_config,
    get_config_snapshot,
    validate_config_update,
)
from robotsix_mill.config import Settings


# ---------------------------------------------------------------------------
#  Module import — no broker contact
# ---------------------------------------------------------------------------


class TestPackageImport:
    """The component_agent package must be importable without contacting
    the broker (no top-level robotsix_agent_comm import)."""

    def test_import_init_clean(self):
        """Importing the package does not import robotsix_agent_comm."""
        import sys

        # Ensure the SDK is not already cached in sys.modules for this test.
        sdk_name = "robotsix_agent_comm"
        was_present = sdk_name in sys.modules
        if was_present:
            saved = sys.modules[sdk_name]
            del sys.modules[sdk_name]

        try:
            # Force a fresh import of the component_agent package.
            # (It may already be cached from prior imports in the suite.)
            pkg = "robotsix_mill.component_agent"
            if pkg in sys.modules:
                del sys.modules[pkg]
            import robotsix_mill.component_agent  # noqa: F401, F811

            assert sdk_name not in sys.modules, (
                "component_agent.__init__ must not import robotsix_agent_comm"
            )
        finally:
            if was_present:
                sys.modules[sdk_name] = saved

    def test_import_config_contract_clean(self):
        """config_contract.py does not import robotsix_agent_comm."""
        import sys

        sdk_name = "robotsix_agent_comm"
        was_present = sdk_name in sys.modules
        if was_present:
            saved = sys.modules[sdk_name]
            del sys.modules[sdk_name]

        try:
            # Force a fresh re-import of config_contract to check it
            # doesn't pull in the SDK.
            module = "robotsix_mill.component_agent.config_contract"
            if module in sys.modules:
                del sys.modules[module]
            import robotsix_mill.component_agent.config_contract  # noqa: F401, F811

            assert sdk_name not in sys.modules, (
                "config_contract must not import robotsix_agent_comm"
            )
        finally:
            if was_present:
                sys.modules[sdk_name] = saved


# ---------------------------------------------------------------------------
#  Secret redaction
# ---------------------------------------------------------------------------


class TestSecretRedaction:
    def test_is_secret_key_matches_token(self):
        assert _is_secret_key("board_agent_broker_token") is True
        assert _is_secret_key("forge_token") is True
        assert _is_secret_key("OPENROUTER_API_KEY") is True

    def test_is_secret_key_matches_password(self):
        assert _is_secret_key("db_password") is True
        assert _is_secret_key("some_password_field") is True

    def test_is_secret_key_matches_secret(self):
        assert _is_secret_key("langfuse_secret_key") is True
        assert _is_secret_key("client_secret") is True

    def test_is_secret_key_rejects_normal_fields(self):
        assert _is_secret_key("data_dir") is False
        assert _is_secret_key("max_concurrency") is False
        assert _is_secret_key("model") is False
        assert _is_secret_key("enabled") is False

    def test_is_secret_key_case_insensitive(self):
        assert _is_secret_key("MY_API_TOKEN") is True
        assert _is_secret_key("My_Api_Key") is True
        assert _is_secret_key("Some_Password") is True

    def test_get_config_snapshot_redacts_all_secrets(self):
        """Every secret-named key is redacted to '***'."""
        s = Settings(
            data_dir="/tmp/test",
            board_agent_enabled=False,
            board_agent_broker_token="real-token-should-not-leak",
            component_agent_broker_token="another-secret",
        )
        snap = get_config_snapshot(s)

        assert snap["data_dir"] == "/tmp/test" or str(snap["data_dir"]) == "/tmp/test"
        assert snap["board_agent_broker_token"] == "***"
        assert snap["component_agent_broker_token"] == "***"

    def test_get_config_snapshot_redacts_forge_token(self):
        s = Settings(forge_token="ghp_secret123")
        snap = get_config_snapshot(s)
        assert snap["forge_token"] == "***"


# ---------------------------------------------------------------------------
#  describe_config
# ---------------------------------------------------------------------------


class TestDescribeConfig:
    def test_returns_settable_dict(self):
        desc = describe_config()
        assert "settable" in desc
        assert isinstance(desc["settable"], dict)

    def test_all_settable_keys_appear(self):
        desc = describe_config()
        settable = desc["settable"]
        # Every SETTABLE_KEYS key that exists on Settings should appear.
        for key in SETTABLE_KEYS:
            if key in Settings.model_fields:
                assert key in settable, f"{key} missing from describe_config"

    def test_each_entry_has_type(self):
        desc = describe_config()
        for key, info in desc["settable"].items():
            assert "type" in info, f"{key} missing type"
            assert isinstance(info["type"], str)


# ---------------------------------------------------------------------------
#  validate_config_update / apply_config_update
# ---------------------------------------------------------------------------


class TestValidateConfigUpdate:
    def test_rejects_key_outside_settable(self):
        s = Settings()
        with pytest.raises(ConfigContractError) as exc_info:
            validate_config_update(s, {"forge_kind": "github"})
        assert exc_info.value.code == "unknown_keys"

    def test_rejects_multiple_unknown_keys(self):
        s = Settings()
        with pytest.raises(ConfigContractError) as exc_info:
            validate_config_update(
                s, {"forge_kind": "github", "api_port": 9999, "data_dir": "/tmp"}
            )
        err = exc_info.value
        assert err.code == "unknown_keys"
        assert "unknown_keys" in err.details
        assert len(err.details["unknown_keys"]) == 3

    def test_rejects_all_startup_only_fields(self):
        """Startup-only fields (forge_*, api_port, data_dir, broker fields,
        enabled flags) must be rejected."""
        s = Settings()
        startup_only = [
            "forge_kind",
            "forge_remote_url",
            "forge_auth",
            "api_port",
            "api_host",
            "data_dir",
            "component_agent_enabled",
            "component_agent_broker_host",
            "component_agent_broker_token",
        ]
        for key in startup_only:
            with pytest.raises(ConfigContractError) as exc_info:
                validate_config_update(s, {key: "dummy_value"})
            assert exc_info.value.code == "unknown_keys"

    def test_does_not_mutate_on_invalid(self):
        s = Settings(data_dir="/original")
        original = s.data_dir
        with pytest.raises(ConfigContractError):
            validate_config_update(s, {"data_dir": "/changed"})
        assert s.data_dir == original, "Settings must not be mutated on invalid input"


class TestApplyConfigUpdate:
    def test_applies_and_returns_audit_map(self):
        s = Settings(max_stuck_cycles=5, requeue_batch_size=100)
        audit = apply_config_update(
            s, {"max_stuck_cycles": 10, "requeue_batch_size": 200}
        )
        assert s.max_stuck_cycles == 10
        assert s.requeue_batch_size == 200
        assert audit == {
            "max_stuck_cycles": (5, 10),
            "requeue_batch_size": (100, 200),
        }

    def test_calls_setter_callback(self):
        s = Settings(max_stuck_cycles=5)
        called_with = []

        def setter(new_s):
            called_with.append(new_s)

        apply_config_update(s, {"max_stuck_cycles": 10}, setter=setter)
        assert len(called_with) == 1
        assert called_with[0] is s

    def test_audit_logs_every_change(self, caplog):
        s = Settings(max_stuck_cycles=5, requeue_batch_size=100)
        with caplog.at_level(logging.INFO, logger="robotsix_mill"):
            apply_config_update(s, {"max_stuck_cycles": 10})

        log_messages = [r.message for r in caplog.records]
        assert any("config-set" in msg for msg in log_messages), (
            "Expected audit log with 'config-set'"
        )

    def test_not_mutate_on_invalid_apply(self):
        s = Settings(max_stuck_cycles=5)
        original = s.max_stuck_cycles
        with pytest.raises(ConfigContractError):
            apply_config_update(s, {"forge_kind": "github"})
        assert s.max_stuck_cycles == original


# ---------------------------------------------------------------------------
#  Worker.snapshot() — read-only, zero side effects
# ---------------------------------------------------------------------------


class TestWorkerSnapshot:
    def test_snapshot_is_read_only(self):
        """Calling snapshot() must NOT start/stop loops or mutate state."""
        from robotsix_mill.runtime.worker import Worker
        from robotsix_mill.core.service import TicketService
        from robotsix_mill.stages import StageContext
        from robotsix_mill.config import RepoConfig

        settings = Settings(data_dir="/tmp/test_snapshot")
        rc = RepoConfig(
            board_id="test",
            repo_id="test-repo",
            langfuse_project_name="p",
            langfuse_public_key="pk",
            langfuse_secret_key="sk",
        )
        svc = TicketService(settings, board_id="test")
        ctx = StageContext(settings=settings, service=svc, repo_config=rc)
        w = Worker(ctx)

        # Before start, tasks should be empty.
        assert w._tasks == []
        snap = w.snapshot()
        assert snap["running"] is False
        assert snap["active_tasks"] == 0
        # Calling snapshot should not have started anything.
        assert w._tasks == []

    @pytest.mark.asyncio
    async def test_snapshot_after_start_shows_running(self):
        """After start(), snapshot reports running=True and active tasks."""
        from robotsix_mill.runtime.worker import Worker
        from robotsix_mill.core.service import TicketService
        from robotsix_mill.stages import StageContext
        from robotsix_mill.config import RepoConfig

        settings = Settings(data_dir="/tmp/test_snapshot2", max_global_concurrency=1)
        rc = RepoConfig(
            board_id="test2",
            repo_id="test-repo2",
            max_concurrency=1,
            langfuse_project_name="p",
            langfuse_public_key="pk",
            langfuse_secret_key="sk",
        )
        svc = TicketService(settings, board_id="test2")
        ctx = StageContext(settings=settings, service=svc, repo_config=rc)
        w = Worker(ctx)

        try:
            w.start()
            snap = w.snapshot()
            assert snap["running"] is True
            assert snap["active_tasks"] >= 1  # at least one consumer
            assert snap["periodic_loops"]  # poll loop should be running
        finally:
            await w.stop()

    def test_snapshot_structure(self):
        from robotsix_mill.runtime.worker import Worker
        from robotsix_mill.core.service import TicketService
        from robotsix_mill.stages import StageContext
        from robotsix_mill.config import RepoConfig

        settings = Settings(data_dir="/tmp/test_snapshot3")
        rc = RepoConfig(
            board_id="test3",
            repo_id="test-repo3",
            langfuse_project_name="p",
            langfuse_public_key="pk",
            langfuse_secret_key="sk",
        )
        svc = TicketService(settings, board_id="test3")
        ctx = StageContext(settings=settings, service=svc, repo_config=rc)
        w = Worker(ctx)

        snap = w.snapshot()
        assert set(snap.keys()) == {
            "running",
            "periodic_loops",
            "active_tasks",
            "queue_depth",
            "in_flight_passes",
        }
        assert isinstance(snap["periodic_loops"], list)
        assert isinstance(snap["active_tasks"], int)
        assert isinstance(snap["queue_depth"], int)
        assert isinstance(snap["in_flight_passes"], int)


# ---------------------------------------------------------------------------
#  ComponentAgentResponder — handler dispatch
# ---------------------------------------------------------------------------


class FakeBrokeredAgent:
    """Minimal fake BrokeredAgent for testing the responder without a
    real broker connection."""

    def __init__(self, agent_id, **kwargs):
        self.agent_id = agent_id
        self.on_request_handler = kwargs.get("on_request")
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class FakeRequest:
    """Lightweight stand-in for agent-comm Request objects.

    Provides the ``body``, ``message_id``, ``sender``, and ``metadata``
    attributes that ``Error.to(...)`` / ``Response.to(...)`` expect.
    """

    def __init__(self, body=None, message_id="msg-1", sender="test-sender"):
        from robotsix_agent_comm.protocol.messages import Metadata  # type: ignore[import-untyped]

        self.body = body or {}
        self.message_id = message_id
        self.sender = sender
        self.metadata = Metadata.create(
            sender=sender, recipient="component-robotsix-mill"
        )


class FakeAppState:
    """Minimal app.state stand-in with the attributes the responder reads."""

    def __init__(self, settings, worker=None, service=None, run_registries=None):
        self.settings = settings
        self.started_at = datetime.datetime.now(datetime.timezone.utc)
        self.worker = worker
        self.service = service
        self.run_registries = run_registries or {}


class TestComponentAgentResponder:
    def test_unknown_kind_returns_error(self):
        """Unknown request kind returns an Error."""
        from robotsix_mill.component_agent.responder import ComponentAgentResponder

        state = FakeAppState(Settings())
        responder = ComponentAgentResponder(
            agent_id="test-agent",
            broker_host="localhost",
            broker_token="test-token",
            app_state=state,
        )

        req = FakeRequest(body={"kind": "bogus-operation"})
        result = responder.on_request(req)

        # result should be an Error with code "unknown_kind"
        assert result is not None
        body = result.body
        assert body["code"] == "unknown_kind"

    def test_monitor_returns_telemetry(self):
        """monitor handler returns uptime and worker snapshot."""
        from robotsix_mill.component_agent.responder import ComponentAgentResponder

        from robotsix_mill.runtime.worker import Worker
        from robotsix_mill.core.service import TicketService
        from robotsix_mill.stages import StageContext
        from robotsix_mill.config import RepoConfig

        settings = Settings(data_dir="/tmp/test_monitor")
        rc = RepoConfig(
            board_id="test-mon",
            repo_id="test-repo-mon",
            langfuse_project_name="p",
            langfuse_public_key="pk",
            langfuse_secret_key="sk",
        )
        svc = TicketService(settings, board_id="test-mon")
        ctx = StageContext(settings=settings, service=svc, repo_config=rc)
        worker = Worker(ctx)

        state = FakeAppState(settings=settings, worker=worker, service=svc)
        responder = ComponentAgentResponder(
            agent_id="test-agent",
            broker_host="localhost",
            broker_token="test-token",
            app_state=state,
        )

        req = FakeRequest(body={"kind": "monitor"})
        result = responder.on_request(req)

        assert result is not None
        payload = result.body
        assert "uptime_seconds" in payload
        assert "worker" in payload
        assert payload["worker"] is not None
        assert "recent_runs" in payload
        assert "ticket_counts" in payload

    def test_config_get_returns_redacted_snapshot(self):
        from robotsix_mill.component_agent.responder import ComponentAgentResponder

        settings = Settings(
            data_dir="/tmp/test_cg",
            component_agent_broker_token="should-be-redacted",
        )
        state = FakeAppState(settings)
        responder = ComponentAgentResponder(
            agent_id="test-agent",
            broker_host="localhost",
            broker_token="test-token",
            app_state=state,
        )

        req = FakeRequest(body={"kind": "config-get"})
        result = responder.on_request(req)

        assert result is not None
        body = result.body
        assert "config" in body
        assert "meta" in body
        assert body["config"]["component_agent_broker_token"] == "***"

    def test_config_set_applies_and_returns_audit(self):
        from robotsix_mill.component_agent.responder import ComponentAgentResponder

        settings = Settings(max_stuck_cycles=5)
        state = FakeAppState(settings)
        responder = ComponentAgentResponder(
            agent_id="test-agent",
            broker_host="localhost",
            broker_token="test-token",
            app_state=state,
        )

        req = FakeRequest(
            body={
                "kind": "config-set",
                "payload": {"updates": {"max_stuck_cycles": 15}},
            }
        )
        result = responder.on_request(req)

        assert result is not None
        body = result.body
        assert "applied" in body
        assert "max_stuck_cycles" in body["applied"]
        # The live settings should be updated
        assert state.settings.max_stuck_cycles == 15

    def test_config_set_rejects_invalid_key(self):
        from robotsix_mill.component_agent.responder import ComponentAgentResponder

        settings = Settings(data_dir="/original")
        state = FakeAppState(settings)
        responder = ComponentAgentResponder(
            agent_id="test-agent",
            broker_host="localhost",
            broker_token="test-token",
            app_state=state,
        )

        req = FakeRequest(
            body={
                "kind": "config-set",
                "payload": {"updates": {"data_dir": "/hacked"}},
            }
        )
        result = responder.on_request(req)

        assert result is not None
        body = result.body
        assert body["code"] == "unknown_keys"
        # Settings must NOT have been mutated
        assert str(state.settings.data_dir) == "/original"

    def test_empty_updates_noop(self):
        from robotsix_mill.component_agent.responder import ComponentAgentResponder

        settings = Settings()
        state = FakeAppState(settings)
        responder = ComponentAgentResponder(
            agent_id="test-agent",
            broker_host="localhost",
            broker_token="test-token",
            app_state=state,
        )

        req = FakeRequest(body={"kind": "config-set", "payload": {}})
        result = responder.on_request(req)

        assert result is not None
        body = result.body
        assert "applied" in body
        assert body["applied"] == {}


# ---------------------------------------------------------------------------
#  Component-agent lifecycle (start/stop gating)
# ---------------------------------------------------------------------------


class TestComponentAgentLifecycle:
    @pytest.mark.asyncio
    async def test_disabled_by_default(self):
        """When component_agent_enabled=False, no start is attempted."""
        from robotsix_mill.runtime.lifespan import (
            _start_component_agent,
        )

        app = MagicMock()
        app.state = MagicMock(
            spec=[
                "started_at",
                "worker",
                "service",
                "settings",
                "run_registry",
                "run_registries",
                "broadcaster",
                "repos",
                "single_repo_id",
            ]
        )
        # Ensure we can detect attribute setting on app.state
        app.state.component_agent = None

        settings = Settings(component_agent_enabled=False)
        # Should not raise — the guard returns early.
        await _start_component_agent(app, settings)
        # No component_agent should be stashed (still None from above).
        assert app.state.component_agent is None

    @pytest.mark.asyncio
    async def test_skips_when_host_empty(self):
        """When enabled but broker_host is empty, Settings construction fails
        (enforced by _validate_cross_field). The lifespan guard is belt-and-suspenders."""
        # The invariant in _validate_cross_field raises at construction time.
        with pytest.raises(ValueError, match="component_agent_broker_host"):
            Settings(
                component_agent_enabled=True,
                component_agent_broker_host="",
                component_agent_broker_token="some-token",
            )

    @pytest.mark.asyncio
    async def test_skips_when_sdk_absent(self):
        """When enabled but robotsix_agent_comm is not installed, skips."""
        import sys
        import importlib

        # Remove the SDK from sys.modules to simulate absence.
        sdk = "robotsix_agent_comm"
        saved = sys.modules.get(sdk)
        if saved is not None:
            del sys.modules[sdk]

        # Also ensure find_spec returns None.
        original_find_spec = importlib.util.find_spec

        def fake_find_spec(name, *a, **kw):
            if name == sdk:
                return None
            return original_find_spec(name, *a, **kw)

        try:
            importlib.util.find_spec = fake_find_spec

            from robotsix_mill.runtime.lifespan import _start_component_agent

            app = MagicMock()
            app.state = MagicMock()
            settings = Settings(
                component_agent_enabled=True,
                component_agent_broker_host="broker.example.com",
                component_agent_broker_token="token",
            )
            with patch.object(
                logging.getLogger("robotsix_mill.runtime.lifespan"), "warning"
            ) as mock_warn:
                await _start_component_agent(app, settings)
                mock_warn.assert_called_once()
                assert "not installed" in mock_warn.call_args[0][0].lower()
        finally:
            importlib.util.find_spec = original_find_spec
            if saved is not None:
                sys.modules[sdk] = saved


# ---------------------------------------------------------------------------
#  Settings invariant: component_agent_enabled requires host + token
# ---------------------------------------------------------------------------


class TestComponentAgentSettingsInvariant:
    def test_enabled_without_host_raises(self):
        with pytest.raises(ValueError, match="component_agent_broker_host"):
            Settings(
                component_agent_enabled=True,
                component_agent_broker_token="token",
                component_agent_broker_host="",
            )

    def test_enabled_without_token_raises(self):
        with pytest.raises(ValueError, match="component_agent_broker_token"):
            Settings(
                component_agent_enabled=True,
                component_agent_broker_host="broker.example.com",
                component_agent_broker_token="",
            )

    def test_enabled_with_host_and_token_ok(self):
        s = Settings(
            component_agent_enabled=True,
            component_agent_broker_host="broker.example.com",
            component_agent_broker_token="secret",
        )
        assert s.component_agent_enabled is True

    def test_disabled_without_host_ok(self):
        """When disabled, host/token can be empty."""
        s = Settings(
            component_agent_enabled=False,
            component_agent_broker_host="",
            component_agent_broker_token="",
        )
        assert s.component_agent_enabled is False

    def test_default_settings_pass_validation(self):
        """Default Settings() must construct without error."""
        s = Settings()
        assert s.component_agent_enabled is False


# ---------------------------------------------------------------------------
#  Config contract — SETTABLE_KEYS sanity
# ---------------------------------------------------------------------------


class TestSettableKeys:
    def test_no_startup_only_fields_in_settable(self):
        """SETTABLE_KEYS must not include forge_*, api_port, data_dir,
        broker connection fields, or enabled flags."""
        forbidden_prefixes = (
            "board_agent_broker_",
            "board_manager_broker_",
            "component_agent_broker_",
            "github_",
            "gitlab_",
        )
        forbidden_exact = {
            "api_host",
            "api_port",
            "api_url",
            "data_dir",
            "default_repo_id",
            "branch_prefix",
            "command_timeout",
            "board_agent_enabled",
            "board_manager_enabled",
            "component_agent_enabled",
            "test_command",
            "smoke_command",
            "shutdown_grace_seconds",
            # forge_ connection fields (not the parity periodic flag)
            "forge_kind",
            "forge_remote_url",
            "forge_auth",
            "forge_token",
            "forge_target_branch",
            "forge_repo_create_token",
            # sandbox provisioning fields (not the reaper periodic flag)
            "sandbox_image",
            "sandbox_memory",
            "sandbox_pids_limit",
            "sandbox_readonly",
            "sandbox_network",
            "sandbox_proxy_url",
            "sandbox_data_mount",
        }
        for key in SETTABLE_KEYS:
            assert key not in forbidden_exact, (
                f"{key} is startup-only and must not be in SETTABLE_KEYS"
            )
            for prefix in forbidden_prefixes:
                assert not key.startswith(prefix), (
                    f"{key} starts with forbidden prefix {prefix!r}"
                )

    def test_all_settable_keys_exist_on_settings(self):
        """Every key in SETTABLE_KEYS must be a real field on Settings."""
        fields = Settings.model_fields
        for key in SETTABLE_KEYS:
            assert key in fields, (
                f"SETTABLE_KEYS contains {key!r} which is not a Settings field"
            )
