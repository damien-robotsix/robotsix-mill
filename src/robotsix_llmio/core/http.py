"""Timeout-bounded async HTTP client for provider SDKs."""

from __future__ import annotations

import asyncio
import weakref
from typing import Any

from . import constants


def _close_async_client(client: "Any") -> None:
    """Close an httpx.AsyncClient from outside its original event loop.

    Creates a temporary event loop to run aclose(), swallowing errors so
    cleanup never raises in a finally/__del__ context.
    """
    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(client.aclose())
        loop.close()
    except Exception:
        pass


def timeout_http_client():
    """A fresh ``httpx.AsyncClient`` with a hard per-request timeout, so a
    hung/glacial provider connection raises instead of blocking forever.
    Pass to the provider as its ``http_client``.
    """
    import httpx

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            constants.MODEL_REQUEST_TIMEOUT, connect=constants.CONNECT_TIMEOUT
        )
    )
    weakref.finalize(client, _close_async_client, client)
    return client
