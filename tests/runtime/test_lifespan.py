import logging

import pytest
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI

from robotsix_mill.config import ReposRegistry
from robotsix_mill.runtime.lifespan import create_lifespan, setup_logging


# ---------------------------------------------------------------------------
# setup_logging
# ---------------------------------------------------------------------------


class TestSetupLogging:
    def test_adds_stream_handler_with_mill_marker(self):
        root = logging.getLogger("robotsix_mill")
        # Remove any existing handlers so we start clean.
        for h in list(root.handlers):
            root.removeHandler(h)

        setup_logging()

        handlers = root.handlers
        assert any(
            isinstance(h, logging.StreamHandler) and getattr(h, "_mill", False)
            for h in handlers
        ), "Expected a StreamHandler with _mill=True"

    def test_idempotent_no_duplicate_handlers(self):
        root = logging.getLogger("robotsix_mill")
        for h in list(root.handlers):
            root.removeHandler(h)

        setup_logging()
        count_after_first = len(root.handlers)
        setup_logging()

        assert (
            len(root.handlers) == count_after_first
        ), "setup_logging should be idempotent"


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
    monkeypatch.setattr("robotsix_mill.runtime.lifespan.Worker", mock_worker_class)

    mock_dr_instance = MagicMock()
    mock_dr_class = MagicMock(return_value=mock_dr_instance)
    monkeypatch.setattr(
        "robotsix_mill.runtime.lifespan.DeepReviewStore", mock_dr_class
    )

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
        "dr_instance": mock_dr_instance,
        "dr_class": mock_dr_class,
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
        assert app.state.run_registries == {
            "test-board": lifespan_mocks["rr_instance"]
        }
        assert app.state.deep_review_results == {}
        assert app.state.deep_review_store is lifespan_mocks["dr_instance"]

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
        assert set(registries.keys()) == {"board-a", "board-b"}

    # Two RunRegistry instances should have been created.
    assert lifespan_mocks["rr_class"].call_count == 2
    # Both calls should have received a Path ending with the right
    # board-id / runs.json.
    calls = lifespan_mocks["rr_class"].call_args_list
    paths = [c[0][0] for c in calls]
    assert any("board-a" in str(p) and p.name == "runs.json" for p in paths)
    assert any("board-b" in str(p) and p.name == "runs.json" for p in paths)


@pytest.mark.asyncio
async def test_create_lifespan_deep_review_store_path(
    settings, repos_registry, lifespan_mocks
):
    """DeepReviewStore is constructed with the correct data-dir path."""
    lifespan = create_lifespan(settings, repos_registry)
    app = FastAPI()

    async with lifespan(app):
        pass

    lifespan_mocks["dr_class"].assert_called_once()
    dr_path = lifespan_mocks["dr_class"].call_args[0][0]
    assert dr_path.name == "deep_review_results.json"
    assert str(settings.data_dir) in str(dr_path)



