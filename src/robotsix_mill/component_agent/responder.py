"""Component-agent responder: monitor / config-get / config-set.

Defines ``ComponentAgentResponder`` with a single synchronous
``on_request(self, request) -> Message | None`` handler that dispatches
on ``request.body["kind"]`` over the three typed kinds.

Lazy-imports ``BrokeredAgent``, ``Response``, ``Error`` INSIDE
``start()`` / the handler so the package imports cleanly even if the SDK
were ever absent.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..config.settings import Settings
from robotsix_agent_comm.protocol import ConfigContractError

from .config_contract import (
    apply_config_update,
    describe_config,
    get_config_snapshot,
)

logger = logging.getLogger("robotsix_mill.component_agent")


class ComponentAgentResponder:
    """Generic component-agent responder: monitor + live-config over the broker.

    Construct with live state from ``app.state`` so the handlers operate on
    real runtime telemetry and can swap live settings on ``config-set``.

    Parameters
    ----------
    agent_id:
        Agent id registered on the broker (default ``"component-robotsix-mill"``).
    broker_host / broker_port / broker_scheme / broker_token:
        Broker connection coordinates.
    app_state:
        The FastAPI ``app.state`` object — the responder reads
        ``.started_at``, ``.worker``, ``.run_registries``, ``.service``,
        and ``.settings`` from it at request time.
    """

    def __init__(
        self,
        agent_id: str,
        *,
        broker_host: str,
        broker_port: int = 443,
        broker_scheme: str = "https",
        broker_token: str,
        app_state: Any,
    ) -> None:
        self.agent_id = agent_id
        self._broker_host = broker_host
        self._broker_port = broker_port
        self._broker_scheme = broker_scheme
        self._broker_token = broker_token
        self._app_state = app_state
        self._agent: Any = None
        self._started = False

    # ------------------------------------------------------------------
    #  Lifecycle (mirrors _start_board_agent / _stop_board_agent)
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Construct the BrokeredAgent and start it in a background thread."""
        # Lazy-import inside start() so the package is importable without
        # the SDK present.
        from robotsix_agent_comm.sdk.brokered import BrokeredAgent

        self._agent = BrokeredAgent(
            self.agent_id,
            broker_host=self._broker_host,
            broker_port=self._broker_port,
            broker_scheme=self._broker_scheme,
            broker_token=self._broker_token,
            on_request=self.on_request,
        )
        # start() is synchronous — run it in a thread to avoid blocking
        # the event loop (mirrors _start_board_agent pattern).
        await asyncio.to_thread(self._agent.start)
        self._started = True
        logger.info(
            "ComponentAgentResponder %r started on broker %s:%d",
            self.agent_id,
            self._broker_host,
            self._broker_port,
        )

    async def stop(self) -> None:
        """Stop the BrokeredAgent if it was started."""
        if self._agent is not None and self._started:
            await asyncio.to_thread(self._agent.stop)
            self._started = False
            logger.info("ComponentAgentResponder %r stopped", self.agent_id)

    # ------------------------------------------------------------------
    #  Request handler
    # ------------------------------------------------------------------

    def on_request(self, request: Any) -> Any | None:
        """Synchronous handler dispatched by the BrokeredAgent.

        Reads ``request.body["kind"]`` and dispatches to the appropriate
        typed handler.  Unknown kind → ``Error.to(...)``.

        Lazy-imports Response/Error inside the handler so the module-level
        import stays clean.
        """
        from robotsix_agent_comm.protocol.messages import Error

        body: dict[str, Any] = getattr(request, "body", None) or {}
        kind: str = body.get("kind", "")

        try:
            if kind == "monitor":
                return self._handle_monitor(request, body)
            elif kind == "config-get":
                return self._handle_config_get(request, body)
            elif kind == "config-set":
                return self._handle_config_set(request, body)
            else:
                return Error.to(
                    request,
                    code="unknown_kind",
                    message=f"Unknown request kind: {kind!r}. "
                    f"Expected one of: monitor, config-get, config-set.",
                    sender=self.agent_id,
                )
        except ConfigContractError as exc:
            return Error.to(
                request,
                code=exc.code,
                message=exc.message,
                sender=self.agent_id,
                **exc.details,
            )
        except Exception:
            logger.exception(
                "ComponentAgentResponder: unhandled error serving %s", kind
            )
            return Error.to(
                request,
                code="internal_error",
                message="Internal error serving request",
                sender=self.agent_id,
            )

    # ------------------------------------------------------------------
    #  Typed handlers
    # ------------------------------------------------------------------

    def _handle_monitor(self, request: Any, body: dict[str, Any]) -> Any:
        """Build the monitor payload from live telemetry on app.state."""
        from robotsix_agent_comm.protocol.messages import Response

        state = self._app_state
        payload: dict[str, Any] = {}

        # Process uptime
        started_at = getattr(state, "started_at", None)
        if started_at is not None:
            import datetime

            now = datetime.datetime.now(datetime.timezone.utc)
            delta = now - started_at
            payload["uptime_seconds"] = int(delta.total_seconds())
            payload["started_at"] = started_at.isoformat()
        else:
            payload["uptime_seconds"] = None
            payload["started_at"] = None

        # Worker snapshot (read-only, zero side effects)
        worker = getattr(state, "worker", None)
        if worker is not None and hasattr(worker, "snapshot"):
            payload["worker"] = worker.snapshot()
        else:
            payload["worker"] = None

        # Recent run activity from per-repo run registries
        run_registries = getattr(state, "run_registries", None)
        if run_registries is not None:
            runs: dict[str, list[dict[str, Any]]] = {}
            for board_id, registry in run_registries.items():
                all_runs = registry.list_all()
                runs[board_id] = all_runs[:10]  # last 10 per board
            payload["recent_runs"] = runs
        else:
            payload["recent_runs"] = None

        # Ticket counts — use existing read-only TicketService query methods
        service = getattr(state, "service", None)
        if service is not None:
            try:
                all_tickets = service.list()
                total = len(all_tickets)
                by_state: dict[str, int] = {}
                for t in all_tickets:
                    s = str(t.state) if t.state else "unknown"
                    by_state[s] = by_state.get(s, 0) + 1
                payload["ticket_counts"] = {"total": total, "by_state": by_state}
            except Exception:
                logger.exception("monitor: failed to read ticket counts")
                payload["ticket_counts"] = None
        else:
            payload["ticket_counts"] = None

        return Response.to(request, body=payload, sender=self.agent_id)

    def _handle_config_get(self, request: Any, body: dict[str, Any]) -> Any:
        """Return a secret-redacted flat config snapshot."""
        from robotsix_agent_comm.protocol.messages import Response

        settings = self._app_state.settings
        snapshot = get_config_snapshot(settings)
        meta = describe_config()
        return Response.to(
            request,
            body={"config": snapshot, "meta": meta},
            sender=self.agent_id,
        )

    def _handle_config_set(self, request: Any, body: dict[str, Any]) -> Any:
        """Validate and apply live-settable config updates."""
        from robotsix_agent_comm.protocol.messages import Response

        payload: dict[str, Any] = body.get("payload", {}) or {}
        updates: dict[str, Any] = payload.get("updates", {}) or {}

        if not updates:
            return Response.to(
                request,
                body={"applied": {}, "message": "No updates provided"},
                sender=self.agent_id,
            )

        settings = self._app_state.settings

        def _setter(s: Settings) -> None:
            self._app_state.settings = s

        audit = apply_config_update(settings, updates, setter=_setter)

        # Build a safe audit for the wire (redact secret values)
        safe_audit: dict[str, dict[str, Any]] = {}
        for key, (old_val, new_val) in audit.items():
            safe_audit[key] = {"old": repr(old_val), "new": repr(new_val)}

        return Response.to(
            request,
            body={"applied": safe_audit},
            sender=self.agent_id,
        )
