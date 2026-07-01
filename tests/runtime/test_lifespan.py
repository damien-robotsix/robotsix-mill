import logging

import pytest
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI

from robotsix_mill.runtime.lifespan import (
    _export_openrouter_key_to_env,
    create_lifespan,
    setup_logging,
)


# ---------------------------------------------------------------------------
# setup_logging
# ---------------------------------------------------------------------------


class TestSetupLogging:
    def test_adds_stream_handler_and_keeps_propagate(self):
        root = logging.getLogger("robotsix_mill")
        # Remove any existing handlers so we start clean.
        for h in list(root.handlers):
            root.removeHandler(h)

        setup_logging()

        assert any(isinstance(h, logging.StreamHandler) for h in root.handlers), (
            "Expected a StreamHandler to be attached"
        )
        assert root.propagate is True

    def test_idempotent_no_duplicate_handlers(self):
        root = logging.getLogger("robotsix_mill")
        for h in list(root.handlers):
            root.removeHandler(h)

        setup_logging()
        count_after_first = len(root.handlers)
        setup_logging()

        assert len(root.handlers) == count_after_first, (
            "setup_logging should be idempotent"
        )

    def test_adds_request_id_log_filter(self):
        """After setup_logging, the handler carries a RequestIDLogFilter."""
        root = logging.getLogger("robotsix_mill")
        for h in list(root.handlers):
            root.removeHandler(h)

        setup_logging()

        from robotsix_mill.runtime.middleware import RequestIDLogFilter

        handler = root.handlers[0]
        assert any(isinstance(f, RequestIDLogFilter) for f in handler.filters), (
            "Expected a RequestIDLogFilter on the handler"
        )

    def test_formatter_includes_request_id(self):
        """After setup_logging, the handler formatter includes %(request_id)s."""
        root = logging.getLogger("robotsix_mill")
        for h in list(root.handlers):
            root.removeHandler(h)

        setup_logging()

        handler = root.handlers[0]
        assert handler.formatter is not None
        assert "%(request_id)s" in handler.formatter._fmt, (
            "Formatter should contain request_id field"
        )
        assert "%(trace_id)s" in handler.formatter._fmt, (
            "Formatter should still contain trace_id field"
        )

    def test_formatter_update_is_idempotent_on_fmt(self):
        """Calling setup_logging twice does not mangle the format string."""
        root = logging.getLogger("robotsix_mill")
        for h in list(root.handlers):
            root.removeHandler(h)

        setup_logging()
        fmt_after_first = root.handlers[0].formatter._fmt
        setup_logging()
        fmt_after_second = root.handlers[0].formatter._fmt

        assert fmt_after_first == fmt_after_second, (
            "Formatter should not change on second call"
        )


# ---------------------------------------------------------------------------
# _export_openrouter_key_to_env
# ---------------------------------------------------------------------------


class TestExportOpenRouterKey:
    def test_exports_key_when_env_unset(self, monkeypatch):
        """When OPENROUTER_API_KEY is unset, it is populated from secrets."""
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setattr(
            "robotsix_mill.config.secrets.get_secrets",
            lambda: MagicMock(openrouter_api_key="sk-secret"),
        )

        _export_openrouter_key_to_env()

        import os

        assert os.environ["OPENROUTER_API_KEY"] == "sk-secret"

    def test_does_not_override_existing_env(self, monkeypatch):
        """An externally-provided env var always wins (setdefault)."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-external")
        monkeypatch.setattr(
            "robotsix_mill.config.secrets.get_secrets",
            lambda: MagicMock(openrouter_api_key="sk-secret"),
        )

        _export_openrouter_key_to_env()

        import os

        assert os.environ["OPENROUTER_API_KEY"] == "sk-external"

    def test_no_op_when_secret_missing(self, monkeypatch):
        """No key configured leaves the env untouched (no empty export)."""
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setattr(
            "robotsix_mill.config.secrets.get_secrets",
            lambda: MagicMock(openrouter_api_key=None),
        )

        _export_openrouter_key_to_env()

        import os

        assert "OPENROUTER_API_KEY" not in os.environ


# ---------------------------------------------------------------------------
# create_lifespan
# ---------------------------------------------------------------------------


@pytest.fixture
def lifespan_mocks(monkeypatch):
    """Patch the expensive / side-effectful dependencies of create_lifespan
    and return MagicMock handles so tests can assert on them.
    """
    mock_init_db = MagicMock()
    monkeypatch.setattr("robotsix_mill.runtime.lifespan.db.init_db", mock_init_db)

    mock_worker = MagicMock()
    mock_worker.stop = AsyncMock()
    mock_worker_class = MagicMock(return_value=mock_worker)
    # Preserve the real synthetic-meta-board constant — lifespan keys the
    # dedicated meta registry off Worker._META_BOARD.
    mock_worker_class._META_BOARD = "meta"
    monkeypatch.setattr("robotsix_mill.runtime.lifespan.Worker", mock_worker_class)

    mock_rr_instance = MagicMock()
    mock_rr_class = MagicMock(return_value=mock_rr_instance)
    monkeypatch.setattr("robotsix_mill.runtime.lifespan.RunRegistry", mock_rr_class)

    mock_install_signals = MagicMock()
    monkeypatch.setattr(
        "robotsix_mill.runtime.lifespan.tracing.install_signal_handlers",
        mock_install_signals,
    )

    return {
        "init_db": mock_init_db,
        "worker": mock_worker,
        "worker_class": mock_worker_class,
        "rr_instance": mock_rr_instance,
        "rr_class": mock_rr_class,
        "install_signals": mock_install_signals,
    }


@pytest.mark.asyncio
async def test_create_lifespan_multi_repo_sets_app_state(
    settings, repos_registry, lifespan_mocks
):
    """With single_repo_id=None (multi-repo), the lifespan picks the
    first repo and stores all expected attributes on app.state."""
    lifespan = create_lifespan(settings, repos_registry)
    app = FastAPI()

    async with lifespan(app):
        assert app.state.settings is settings
        assert app.state.repos is repos_registry
        assert app.state.single_repo_id is None
        assert app.state.worker is lifespan_mocks["worker"]
        assert app.state.run_registry is lifespan_mocks["rr_instance"]
        # A dedicated synthetic "meta" registry is always added alongside
        # the per-repo registries (even in single-repo mode).
        assert app.state.run_registries == {
            "test-board": lifespan_mocks["rr_instance"],
            "meta": lifespan_mocks["rr_instance"],
        }

    # Startup assertions
    lifespan_mocks["init_db"].assert_called_once_with(settings, "test-board")
    lifespan_mocks["worker_class"].assert_called_once()
    lifespan_mocks["worker"].start.assert_called_once()
    lifespan_mocks["worker"].requeue_unfinished.assert_called_once()
    lifespan_mocks["install_signals"].assert_called_once()

    # Shutdown assertion
    lifespan_mocks["worker"].stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_lifespan_single_repo_picks_correct_repo(
    settings, two_repo_registry, lifespan_mocks
):
    """With single_repo_id='repo-b', the lifespan uses repo-b's config
    for the worker and services, and calls init_db for both repos."""
    lifespan = create_lifespan(settings, two_repo_registry, single_repo_id="repo-b")
    app = FastAPI()

    async with lifespan(app):
        assert app.state.single_repo_id == "repo-b"
        # The worker's context should use repo-b's board_id.
        assert app.state.run_registry is lifespan_mocks["rr_instance"]

    # init_db must be called for every registered repo.
    assert lifespan_mocks["init_db"].call_count == 2
    lifespan_mocks["init_db"].assert_any_call(settings, "board-a")
    lifespan_mocks["init_db"].assert_any_call(settings, "board-b")


@pytest.mark.asyncio
async def test_create_lifespan_per_repo_run_registries(
    settings, two_repo_registry, lifespan_mocks
):
    """In multi-repo mode each repo gets its own RunRegistry with the
    correct file path."""
    lifespan = create_lifespan(settings, two_repo_registry)
    app = FastAPI()

    async with lifespan(app):
        registries = app.state.run_registries
        # Per-repo registries plus the synthetic meta-board registry.
        assert set(registries.keys()) == {"board-a", "board-b", "meta"}

    # Three RunRegistry instances should have been created (two repos +
    # the dedicated meta board).
    assert lifespan_mocks["rr_class"].call_count == 3
    # Each call should have received a Path ending with the right
    # board-id / runs.json.
    calls = lifespan_mocks["rr_class"].call_args_list
    paths = [c[0][0] for c in calls]
    assert any("board-a" in str(p) and p.name == "runs.json" for p in paths)
    assert any("board-b" in str(p) and p.name == "runs.json" for p in paths)
    assert any(
        str(p).endswith("meta/runs.json") and p.name == "runs.json" for p in paths
    )


# ---------------------------------------------------------------------------
# Deploy-mode sandbox wiring (DOCKER_HOST gate)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_mode_wires_network_and_volume(
    settings, repos_registry, lifespan_mocks, monkeypatch
):
    """With DOCKER_HOST set, startup resolves the data volume and creates
    the egress network before the worker starts."""
    monkeypatch.setenv("DOCKER_HOST", "tcp://mill-socket-proxy:2375")
    resolve = MagicMock()
    ensure = MagicMock(return_value=True)
    monkeypatch.setattr("robotsix_mill.sandbox.resolve_data_volume", resolve)
    monkeypatch.setattr("robotsix_mill.sandbox.ensure_sandbox_network", ensure)

    lifespan = create_lifespan(settings, repos_registry)
    app = FastAPI()
    async with lifespan(app):
        pass

    resolve.assert_called_once_with(settings)
    ensure.assert_called_once_with(settings)


@pytest.mark.asyncio
async def test_dev_mode_skips_network_and_volume(
    settings, repos_registry, lifespan_mocks, monkeypatch
):
    """Without DOCKER_HOST (dev stack), startup never touches the deploy
    helpers — the dev path is unchanged."""
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    resolve = MagicMock()
    ensure = MagicMock(return_value=True)
    monkeypatch.setattr("robotsix_mill.sandbox.resolve_data_volume", resolve)
    monkeypatch.setattr("robotsix_mill.sandbox.ensure_sandbox_network", ensure)

    lifespan = create_lifespan(settings, repos_registry)
    app = FastAPI()
    async with lifespan(app):
        pass

    resolve.assert_not_called()
    ensure.assert_not_called()


# ---------------------------------------------------------------------------
# Zero-repo startup (healthy start with no repos configured)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_lifespan_zero_repos(settings, lifespan_mocks):
    """Server starts healthy with no repos — meta board is used as fallback."""
    from robotsix_mill.config import ReposRegistry
    from robotsix_mill.runtime.worker import Worker

    empty_repos = ReposRegistry(repos={})
    lifespan = create_lifespan(settings, empty_repos)
    app = FastAPI()

    async with lifespan(app):
        assert app.state.repos is empty_repos
        assert app.state.single_repo_id is None
        assert app.state.worker is lifespan_mocks["worker"]
        # Service falls back to the synthetic meta board.
        service = app.state.service
        assert service.board_id == Worker._META_BOARD
        # Only the meta board registry exists (no per-repo registries).
        assert set(app.state.run_registries.keys()) == {"meta"}
        assert app.state.run_registry is lifespan_mocks["rr_instance"]

    # meta board DB initialized (not per-repo DBs — there are none).
    lifespan_mocks["init_db"].assert_called_once_with(settings, Worker._META_BOARD)
    # Worker still starts and requeues.
    lifespan_mocks["worker"].start.assert_called_once()
    lifespan_mocks["worker"].requeue_unfinished.assert_called_once()
    # Board-agent and board-manager NOT started.
    assert not hasattr(app.state, "board_agent")
    assert not hasattr(app.state, "board_manager")


# ---------------------------------------------------------------------------
# BoardManager constructor compatibility (regression guard)
# ---------------------------------------------------------------------------
def test_board_manager_signature_accepts_mill_kwargs():
    """The REAL board-agent ``BoardManager.__init__`` must accept the exact
    kwargs ``_start_board_manager`` builds.

    Regression: on 2026-07-01 a board-agent pin bump dropped ``openrouter_key``
    from the constructor while ``lifespan.py`` still passed it, crashing app
    startup (``TypeError: unexpected keyword argument 'openrouter_key'``). Every
    unit test mocked ``BoardManager``, so nothing exercised the real signature
    and the break only surfaced at deploy. This binds mill's kwargs against the
    installed board-agent signature so an incompatible pin fails in CI instead.

    Keep this kwarg set in sync with ``_start_board_manager`` in lifespan.py.
    """
    import inspect
    from pathlib import Path

    from robotsix_board_agent.board_manager import BoardManager
    from robotsix_board_agent.config import BoardAgentSettings

    agent_settings = BoardAgentSettings(
        board_api_url="http://x",
        board_api_token="t",
        board_repo_id="r",
        enable_write_ops=True,
    )
    manager_kwargs = dict(
        broker_host="h",
        broker_port=443,
        broker_scheme="https",
        broker_token="bt",
        memory_path=Path("/tmp/board_manager_memory.json"),
        agent_id="board-manager-r",
        manager_model=None,
        recall_model=None,
        max_conversations=200,
    )
    # Raises TypeError if any kwarg is unknown / a required param is missing —
    # the exact failure mode of the outage.
    inspect.signature(BoardManager.__init__).bind(
        None, agent_settings, **manager_kwargs
    )
