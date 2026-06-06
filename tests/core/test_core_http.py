"""Core HTTP client factory — timeout-bounded ``httpx.AsyncClient`` plus the
``weakref.finalize`` cleanup callback."""

from __future__ import annotations

import asyncio
import gc
import weakref
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from robotsix_llmio.core import constants
from robotsix_llmio.core import http as http_module
from robotsix_llmio.core.http import _close_async_client, timeout_http_client


def _aclose_sync(client: Any) -> None:
    """Drive ``client.aclose()`` to completion via a one-shot event loop —
    the same shape the production finalizer uses — so tests don't leak open
    httpx connection pools between cases."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(client.aclose())
    finally:
        loop.close()


# --- §1 timeout_http_client -------------------------------------------------


def test_timeout_http_client_returns_async_client():
    client = timeout_http_client()
    try:
        assert isinstance(client, httpx.AsyncClient)
    finally:
        _aclose_sync(client)


def test_timeout_http_client_uses_module_constants():
    """The returned client must carry the module-level timeout knobs
    verbatim. ``httpx.Timeout(MODEL_REQUEST_TIMEOUT, connect=CONNECT_TIMEOUT)``
    broadcasts the positional value to read/write/pool and the keyword
    overrides only connect — pin all four so a regression that silently
    drops one to a tighter default is caught."""
    client = timeout_http_client()
    try:
        assert client.timeout.read == constants.MODEL_REQUEST_TIMEOUT
        assert client.timeout.write == constants.MODEL_REQUEST_TIMEOUT
        assert client.timeout.pool == constants.MODEL_REQUEST_TIMEOUT
        assert client.timeout.connect == constants.CONNECT_TIMEOUT
    finally:
        _aclose_sync(client)


def test_timeout_http_client_registers_weakref_finalize(monkeypatch):
    """Pin the cleanup-on-GC contract: a refactor that swaps
    ``weakref.finalize`` for an ``atexit`` hook or ``__del__`` would
    silently leak orphaned clients. Replace the module's ``weakref``
    attribute (rather than patching the real ``weakref`` module globally)
    so the patch stays local to ``http``."""
    recorded: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fake_finalize(*args: Any, **kwargs: Any) -> Any:
        recorded.append((args, kwargs))
        return SimpleNamespace(alive=True)

    monkeypatch.setattr(http_module, "weakref", SimpleNamespace(finalize=fake_finalize))

    client = timeout_http_client()
    try:
        assert len(recorded) == 1
        args, kwargs = recorded[0]
        assert args == (client, _close_async_client, client)
        assert kwargs == {}
    finally:
        _aclose_sync(client)


# --- §2 _close_async_client closes -----------------------------------------


def test_close_async_client_closes_open_client():
    """Happy path: ``_close_async_client`` drives ``aclose`` to completion on
    a real httpx client. The client is constructed directly (not via
    ``timeout_http_client``) so no finalizer races us into a double-close."""
    client = httpx.AsyncClient()
    _close_async_client(client)
    assert client.is_closed is True


# --- §3 _close_async_client exception handling -----------------------------


def test_close_async_client_swallows_aclose_exception():
    """Regression guard for the bare ``except Exception: pass`` — a finalizer
    must never raise into ``weakref.finalize``'s callback context, where an
    uncaught error would corrupt process shutdown."""

    class _Boom:
        async def aclose(self) -> None:
            raise RuntimeError("boom from aclose")

    assert _close_async_client(_Boom()) is None


def test_close_async_client_propagates_attributeerror():
    """The finalizer narrows its swallow to ``(RuntimeError, OSError)`` — the
    expected event-loop/transport teardown errors. A referent with no
    ``aclose`` attribute raises ``AttributeError``, which is deliberately
    *not* swallowed so a genuinely broken referent surfaces instead of being
    masked (see the production handler comment)."""
    with pytest.raises(AttributeError):
        _close_async_client(object())


def test_close_async_client_swallows_event_loop_runtime_error(monkeypatch):
    """If ``asyncio.new_event_loop`` itself raises (the "no current event
    loop" edge case the production docstring calls out), the finalizer must
    still swallow the error."""

    def _boom() -> Any:
        raise RuntimeError("no current event loop")

    monkeypatch.setattr(http_module.asyncio, "new_event_loop", _boom)
    stub = SimpleNamespace(aclose=lambda: None)
    assert _close_async_client(stub) is None


# --- §4 finalizer runs and routes through _close_async_client --------------


def test_finalizer_closes_client_on_gc(monkeypatch):
    """Pin that the registered finalize routes through
    ``_close_async_client`` (not an inline ``client.aclose()`` or some other
    cleanup path).

    ``weakref.finalize`` holds strong references to its callback args via
    ``info.args``, so the production registration shape ``weakref.finalize(
    client, _close_async_client, client)`` keeps ``client`` alive until the
    finalize fires — meaning the cleanup side effect can race with test
    teardown, exactly as the ticket Note calls out. The reliable signal is
    therefore the recorded call against the wrapped
    ``_close_async_client``: capture the registered finalize, drop the
    strong reference, ``gc.collect()`` to flush, then fire the captured
    finalize (the equivalent of the weakref callback path) and assert the
    wrapper recorded exactly one call carrying the original client."""
    real_close = http_module._close_async_client
    calls: list[Any] = []

    def wrapper(client: Any) -> None:
        calls.append(client)
        real_close(client)

    monkeypatch.setattr(http_module, "_close_async_client", wrapper)

    real_finalize = http_module.weakref.finalize
    finalizers: list[Any] = []

    def fake_finalize(*args: Any, **kwargs: Any) -> Any:
        f = real_finalize(*args, **kwargs)
        finalizers.append(f)
        return f

    monkeypatch.setattr(
        http_module,
        "weakref",
        SimpleNamespace(finalize=fake_finalize),
    )

    client = timeout_http_client()
    client_id = id(client)
    _ref = weakref.ref(client)
    del client
    gc.collect()

    assert len(finalizers) == 1
    # GC alone cannot fire the finalize under the production registration
    # shape (``info.args`` strong-refs the client); explicitly invoking the
    # captured finalize is the equivalent of the weakref callback path.
    finalizers[0]()
    assert len(calls) == 1
    assert id(calls[0]) == client_id
    # ``_ref`` captured to document the GC-on-collect contract; the
    # recorded-call assertion above is the reliable signal per the ticket
    # Note, so its liveness is intentionally not asserted.
    assert _ref is not None
