"""FastAPI lifespan management for robotsix-mill.

Provides ``setup_logging()`` (called once at import time) and
``create_lifespan(settings)`` which returns an async context manager
suitable for ``FastAPI(lifespan=...)``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncContextManager, Callable

from fastapi import FastAPI
from robotsix_llmio.logging import setup_logging as llmio_setup_logging

from ..config import ReposRegistry, Settings
from ..core import db
from ..core.service import TicketService
from ..stages import StageContext
from . import tracing
from .broadcaster import BoardBroadcaster
from .run_registry import RunRegistry
from .worker import Worker


def setup_logging() -> None:
    """Surface ``robotsix_mill.*`` logs on stdout.

    Without this, app logs (worker/audit/stages/notify) propagate to a
    root logger that uvicorn leaves handler-less, so they vanish from
    docker logs — masking failures (e.g. a silently-crashing /audit
    background thread).  Idempotent.
    """
    # Configure both mill's own logger AND robotsix_llmio's, so the
    # extracted LLM-I/O library's logs (esp. the claude_sdk per-turn stream
    # feedback) surface in docker logs instead of vanishing at a handler-less
    # root.  Delegate to llmio's shared helper (idempotent; attaches a single
    # StreamHandler carrying llmio's trace-id filter + the ``console`` formatter).
    # Pass level=logging.INFO explicitly to preserve mill's always-INFO
    # behavior rather than relying on the helper's LOG_LEVEL env resolution.
    # Note: llmio's ``console`` format orders the fields as
    # ``%(asctime)s %(levelname)s %(name)s [%(trace_id)s] %(message)s`` and
    # renders ``-`` (not ``N/A``) when no span is active — an accepted
    # cosmetic change from mill's previous format.
    llmio_setup_logging(loggers=["robotsix_mill", "robotsix_llmio"], level=logging.INFO)
    # Re-set propagate=True after the helper (which sets it False): uvicorn
    # leaves the real root handler-less so there's no double-logging, and
    # pytest's caplog fixture needs propagation to capture our records.
    logging.getLogger("robotsix_mill").propagate = True
    logging.getLogger("robotsix_llmio").propagate = True

    # Inject request-id into every log record and into the formatter
    # so [%(request_id)s] appears alongside the trace-id field.
    from .middleware import RequestIDLogFilter

    mill_logger = logging.getLogger("robotsix_mill")
    for handler in mill_logger.handlers:
        handler.addFilter(RequestIDLogFilter())
        if handler.formatter is not None and hasattr(handler.formatter, "_fmt"):
            old_fmt = handler.formatter._fmt
            if old_fmt is not None and "%(request_id)s" not in old_fmt:
                new_fmt = old_fmt.replace(
                    "[%(trace_id)s]", "[%(trace_id)s] [%(request_id)s]"
                )
                handler.setFormatter(logging.Formatter(new_fmt))
        break  # only the first (llmio-placed) handler


# Called at import time so logging is configured before any lifespan or
# route code logs — the idempotency guard makes this safe to repeat.
setup_logging()

# Module-level process start time, set at the beginning of the lifespan
# startup phase. Accessible without an ``app`` reference so the
# trace-review runner can import it directly for restart correlation.
_process_started_at: datetime | None = None


def _export_openrouter_key_to_env() -> None:
    """Surface the configured OpenRouter key into ``OPENROUTER_API_KEY``.

    The mill stores the key in ``secrets.yaml`` and passes it *explicitly*
    to its own provider, so it is never exported to the process env. But
    in-process llmio consumers that go through ``build_agent_for_level``
    construct ``OpenRouterDeepseekProvider()`` with no key, so the provider
    falls back to reading ``OPENROUTER_API_KEY`` from the environment. Without
    this export those consumers (e.g. the board-manager's recall agent) fail
    with "OpenRouter API key missing".

    Uses ``setdefault`` so an externally-provided env var always wins.
    """
    from ..config.secrets import get_secrets

    or_key = get_secrets().openrouter_api_key
    if or_key:
        os.environ.setdefault("OPENROUTER_API_KEY", or_key)


def create_lifespan(
    settings: Settings,
    repos: ReposRegistry,
    single_repo_id: str | None = None,
) -> Callable[[FastAPI], AsyncContextManager]:
    """Build a FastAPI lifespan callable that performs the same startup
    and shutdown steps as the original inline ``@asynccontextmanager``:

    - Initialise the DB and tracing.
    - Construct ``TicketService``, ``StageContext``, and ``Worker``.
    - Store them on ``app.state`` for route handlers.
    - Start the worker and requeue unfinished tickets.
    - On shutdown, gracefully stop the worker.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Surface the OpenRouter key into the process env *before* any
        # in-process llmio consumer starts, so ``build_agent_for_level``'s
        # OpenRouter provider (which reads only ``OPENROUTER_API_KEY``) can
        # authenticate process-wide — not just when the board-manager runs.
        _export_openrouter_key_to_env()

        # Initialize each registered repo's DB so per-board services
        # have schema available without lazy-init races.
        # Every ticket lives in a per-repo DB.
        for rc in repos.repos.values():
            db.init_db(settings, rc.board_id)

        # Record process start time for health endpoint and restart
        # correlation in trace-review (incomplete traces ending near
        # this time are likely restart kills, not agent-loop bugs).
        global _process_started_at
        _process_started_at = datetime.now(timezone.utc)
        app.state.started_at = _process_started_at

        # In single-repo mode use the specified repo; in multi-repo mode
        # pick the first repo as the initial repo_config for the worker.
        if single_repo_id is not None:
            repo_config = repos.repos[single_repo_id]
        else:
            repo_config = next(iter(repos.repos.values()))
        service = TicketService(settings, board_id=repo_config.board_id)
        broadcaster = BoardBroadcaster()
        service._on_transition = broadcaster.broadcast_sync
        ctx = StageContext(settings=settings, service=service, repo_config=repo_config)
        app.state.repos = repos
        app.state.single_repo_id = single_repo_id
        app.state.broadcaster = broadcaster
        # Per-repo run registries — each repo's audit/health/etc. run
        # log lands in <data_dir>/<board_id>/runs.json.
        run_registries: dict[str, RunRegistry] = {
            rc.board_id: RunRegistry(
                settings.data_dir / rc.board_id / "runs.json",
            )
            for rc in repos.repos.values()
        }
        # The synthetic cross-repo meta board is not a registered repo
        # (deliberately kept out of ReposRegistry), so the comprehension
        # above never builds a registry for it. Add one explicitly so the
        # meta-agent's periodic runs land on the meta board's runs drawer
        # instead of leaking into the lead repo's. Harmless in single-repo
        # mode (an empty registry that is never queried).
        run_registries[Worker._META_BOARD] = RunRegistry(
            settings.data_dir / Worker._META_BOARD / "runs.json",
        )
        # Default registry for the worker's own (board-less) periodic
        # ticks — points at the lead repo's registry so legacy
        # callers without repo context still record somewhere.
        default_registry = run_registries[repo_config.board_id]
        worker = Worker(ctx, default_registry, run_registries=run_registries)
        app.state.settings = settings
        app.state.service = service
        app.state.worker = worker
        app.state.run_registry = default_registry
        app.state.run_registries = run_registries
        tracing.install_signal_handlers()

        # Reap any sandbox containers orphaned by a previous crash/restart
        # before doing anything else. At startup no sandbox is running yet,
        # so every mill-sbx-*/mill-fetch-* present is an orphan from before
        # this process began — they would otherwise run forever (their
        # timeout is parent-process enforced, and --rm only fires on exit).
        # This is the guaranteed backstop complementing the periodic reaper.
        try:
            from ..sandbox import reap_orphan_sandboxes

            reaped = await asyncio.to_thread(reap_orphan_sandboxes)
            if reaped:
                logging.getLogger(__name__).warning(
                    "startup: reaped %d orphan sandbox container(s)", reaped
                )
        except Exception:
            logging.getLogger(__name__).exception("startup sandbox reap failed")

        worker.start()
        worker.requeue_unfinished()  # resume anything left mid-pipeline

        if settings.board_agent_enabled:
            await _start_board_agent(app, settings, repo_config.board_id)
        if settings.board_manager_enabled:
            await _start_board_manager(app, settings, repo_config.board_id)
        if settings.component_agent_enabled:
            await _start_component_agent(app, settings)

        try:
            yield
        finally:
            if settings.board_manager_enabled:
                await _stop_board_manager(app)
            if settings.board_agent_enabled:
                await _stop_board_agent(app)
            if settings.component_agent_enabled:
                await _stop_component_agent(app)
            await worker.stop()

    return lifespan


async def _start_board_agent(
    app: FastAPI,
    settings: Settings,
    repo_id: str,
) -> None:
    """Start the board-agent agent-comm service.

    Deferred import: only imported when ``board_agent_enabled`` is True,
    so deployments that keep the agent off pay zero import overhead and
    don't need the package installed.
    """
    try:
        from robotsix_board_agent.brokered import BrokeredBoardResponder
        from robotsix_board_agent.config import BoardAgentSettings
    except ImportError as exc:
        logging.getLogger(__name__).warning(
            "board_agent_enabled=True but robotsix-board-agent[prod] is not "
            "installed: %s",
            exc,
        )
        return

    if not settings.board_agent_broker_host:
        logging.getLogger(__name__).warning(
            "board_agent_enabled=True but board_agent_broker_host is unset; "
            "the board agent needs a broker to be reachable — skipping start."
        )
        return

    agent_settings = BoardAgentSettings(
        board_api_url=settings.board_agent_api_url,
        board_api_token=settings.board_agent_api_token,
        board_repo_id=settings.board_agent_repo_id or repo_id,
        enable_write_ops=settings.board_agent_write_ops,
    )

    # Register with the central broker in pull/mailbox mode: outbound-only, so
    # the agent is reachable from off-host clients even behind NAT.
    agent = BrokeredBoardResponder(
        agent_settings,
        broker_host=settings.board_agent_broker_host,
        broker_port=settings.board_agent_broker_port,
        broker_scheme=settings.board_agent_broker_scheme,
        broker_token=settings.board_agent_broker_token,
        agent_id=f"board-{agent_settings.board_repo_id}",
    )
    # start()/stop() are synchronous (the responder owns its own event loop);
    # run the blocking broker registration off the event loop.
    await asyncio.to_thread(agent.start)
    app.state.board_agent = agent


async def _stop_board_agent(app: FastAPI) -> None:
    """Stop the board agent if it was started."""
    agent = getattr(app.state, "board_agent", None)
    if agent is not None:
        await asyncio.to_thread(agent.stop)
        del app.state.board_agent


async def _start_board_manager(
    app: FastAPI,
    settings: Settings,
    repo_id: str,
) -> None:
    """Start the conversational LLM board manager (deferred import).

    Reuses the board API + broker coordinates from the board-agent settings and
    mill's own OpenRouter key; registers on the broker as its own agent.
    """
    try:
        from robotsix_board_agent.board_manager import BoardManager
        from robotsix_board_agent.config import BoardAgentSettings
    except ImportError as exc:
        logging.getLogger(__name__).warning(
            "board_manager_enabled=True but robotsix-board-agent[prod] is not "
            "installed: %s",
            exc,
        )
        return

    if not settings.board_agent_broker_host:
        logging.getLogger(__name__).warning(
            "board_manager_enabled=True but board_agent_broker_host is unset; "
            "the manager needs a broker to be reachable — skipping start."
        )
        return

    from ..config.secrets import get_secrets

    openrouter_key = get_secrets().openrouter_api_key
    if not openrouter_key:
        logging.getLogger(__name__).warning(
            "board_manager_enabled=True but no OpenRouter key is configured — "
            "skipping start."
        )
        return

    board_repo_id = settings.board_agent_repo_id or repo_id
    agent_settings = BoardAgentSettings(
        board_api_url=settings.board_agent_api_url,
        board_api_token=settings.board_agent_api_token,
        board_repo_id=board_repo_id,
        enable_write_ops=settings.board_agent_write_ops,
    )
    manager = BoardManager(
        agent_settings,
        broker_host=settings.board_agent_broker_host,
        broker_port=settings.board_agent_broker_port,
        broker_scheme=settings.board_agent_broker_scheme,
        broker_token=settings.board_manager_broker_token,
        openrouter_key=openrouter_key,
        memory_path=settings.data_dir / board_repo_id / "board_manager_memory.json",
        agent_id=f"board-manager-{board_repo_id}",
        manager_model=settings.board_manager_model or None,
        recall_model=settings.board_manager_recall_model or None,
        max_conversations=settings.board_manager_max_conversations,
    )
    await asyncio.to_thread(manager.start)
    app.state.board_manager = manager


async def _stop_board_manager(app: FastAPI) -> None:
    """Stop the board manager if it was started."""
    manager = getattr(app.state, "board_manager", None)
    if manager is not None:
        await asyncio.to_thread(manager.stop)
        del app.state.board_manager


async def _start_component_agent(
    app: FastAPI,
    settings: Settings,
) -> None:
    """Start the component-agent responder (deferred import).

    Registers a generic monitor/config responder on the broker under
    ``component_agent_agent_id`` (default ``"component-robotsix-mill"``).
    Mirrors the ``_start_board_agent`` pattern: lazy-import, graceful
    degradation when the SDK is absent, host-empty guard.
    """
    import importlib.util

    if not importlib.util.find_spec("robotsix_agent_comm"):
        logging.getLogger(__name__).warning(
            "component_agent_enabled=True but robotsix-agent-comm is not "
            "installed; skipping component-agent start."
        )
        return

    if not settings.component_agent_broker_host:
        logging.getLogger(__name__).warning(
            "component_agent_enabled=True but component_agent_broker_host "
            "is unset; the component agent needs a broker to be reachable "
            "— skipping start."
        )
        return

    from ..component_agent.responder import ComponentAgentResponder

    responder = ComponentAgentResponder(
        agent_id=settings.component_agent_agent_id,
        broker_host=settings.component_agent_broker_host,
        broker_port=settings.component_agent_broker_port,
        broker_scheme=settings.component_agent_broker_scheme,
        broker_token=settings.component_agent_broker_token,
        app_state=app.state,
    )
    await responder.start()
    app.state.component_agent = responder


async def _stop_component_agent(app: FastAPI) -> None:
    """Stop the component agent if it was started."""
    responder = getattr(app.state, "component_agent", None)
    if responder is not None:
        await responder.stop()
        del app.state.component_agent
