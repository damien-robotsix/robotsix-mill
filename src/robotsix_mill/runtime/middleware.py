"""ASGI middleware: request-ID injection and logging integration."""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar
from typing import Any

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIDMiddleware:
    """Pure-ASGI middleware that assigns a unique ID to every HTTP request.

    Extracts ``X-Request-ID`` from the incoming request or generates a
    UUID4 hex string.  Stores the ID in a :class:`ContextVar` (accessible
    anywhere in the request lifecycle) and in ``scope["state"]`` (so
    ``request.state.request_id`` works in route handlers).  Returns the
    ID to the client in the ``X-Request-ID`` response header.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Extract or generate request ID
        request_id = ""
        for key, value in scope.get("headers", []):
            if key == b"x-request-id":
                request_id = value.decode("ascii", errors="replace")
                break
        if not request_id:
            request_id = uuid.uuid4().hex

        # Store in ContextVar (accessible anywhere) and scope state (for
        # request.state.request_id in route handlers)
        token = request_id_var.set(request_id)
        state = scope.setdefault("state", {})
        state["request_id"] = request_id

        async def send_wrapper(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            request_id_var.reset(token)


class RequestIDLogFilter(logging.Filter):
    """stdlib logging filter that injects the current request ID."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True
