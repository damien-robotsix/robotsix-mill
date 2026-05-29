"""Unit tests for robotsix_mill.runtime.transient_errors.

Pure unit tests — no I/O, no async, no fixtures beyond ``monkeypatch``.
Every function in the module gets direct test coverage.
"""

import subprocess
from unittest.mock import Mock

import httpx
import openai

from robotsix_mill.runtime.transient_errors import (
    _is_transient_called_process_error,
    _is_transient_httpx,
    _is_transient_openai,
    classify_stage_error,
)


# ---------------------------------------------------------------------------
# _is_transient_httpx
# ---------------------------------------------------------------------------

_httpx_response_500 = Mock(status_code=500)
_httpx_response_503 = Mock(status_code=503)
_httpx_response_404 = Mock(status_code=404)
_httpx_response_429 = Mock(status_code=429)

_httpx_request = Mock()  # never touched by the function


def test_httpx_transient_connect_error():
    assert _is_transient_httpx(httpx.ConnectError("connection refused")) is True


def test_httpx_transient_read_timeout():
    assert _is_transient_httpx(httpx.ReadTimeout("read")) is True


def test_httpx_transient_remote_protocol_error():
    assert _is_transient_httpx(httpx.RemoteProtocolError("protocol")) is True


def test_httpx_transient_timeout_exception():
    assert _is_transient_httpx(httpx.TimeoutException("timeout")) is True


def test_httpx_transient_transport_error():
    assert _is_transient_httpx(httpx.TransportError("transport")) is True


def test_httpx_transient_http_500():
    exc = httpx.HTTPStatusError(
        "boom", request=_httpx_request, response=_httpx_response_500
    )
    assert _is_transient_httpx(exc) is True


def test_httpx_transient_http_503():
    exc = httpx.HTTPStatusError(
        "boom", request=_httpx_request, response=_httpx_response_503
    )
    assert _is_transient_httpx(exc) is True


def test_httpx_fatal_http_404():
    exc = httpx.HTTPStatusError(
        "boom", request=_httpx_request, response=_httpx_response_404
    )
    assert _is_transient_httpx(exc) is False


def test_httpx_fatal_http_429():
    exc = httpx.HTTPStatusError(
        "boom", request=_httpx_request, response=_httpx_response_429
    )
    assert _is_transient_httpx(exc) is False


def test_httpx_fatal_unrelated_exception():
    assert _is_transient_httpx(ValueError("not httpx")) is False


# ---------------------------------------------------------------------------
# _is_transient_openai
# ---------------------------------------------------------------------------


def test_openai_transient_api_connection_error():
    assert (
        _is_transient_openai(
            openai.APIConnectionError(message="api down", request=_httpx_request)
        )
        is True
    )


def test_openai_transient_rate_limit_error():
    assert (
        _is_transient_openai(
            openai.RateLimitError("rate", response=_httpx_response_500, body=None)
        )
        is True
    )


def test_openai_transient_api_timeout_error():
    assert _is_transient_openai(openai.APITimeoutError(request=_httpx_request)) is True


def test_openai_transient_internal_server_error():
    assert (
        _is_transient_openai(
            openai.InternalServerError("500", response=_httpx_response_500, body=None)
        )
        is True
    )


def test_openai_fatal_unrelated():
    assert _is_transient_openai(ValueError("not openai")) is False


def test_openai_none_when_not_installed(monkeypatch):
    monkeypatch.setattr("robotsix_mill.runtime.transient_errors.openai", None)
    assert (
        _is_transient_openai(
            openai.APIConnectionError(message="api down", request=_httpx_request)
        )
        is False
    )


# ---------------------------------------------------------------------------
# _is_transient_called_process_error
# ---------------------------------------------------------------------------


def test_cpe_transient_git_500():
    exc = subprocess.CalledProcessError(
        1, "git", stderr="remote: Internal Server Error"
    )
    assert _is_transient_called_process_error(exc) is True


def test_cpe_transient_git_503():
    exc = subprocess.CalledProcessError(
        1, "git", stderr="error: 503 Service Unavailable"
    )
    assert _is_transient_called_process_error(exc) is True


def test_cpe_transient_git_connection_refused():
    exc = subprocess.CalledProcessError(1, "git", stderr="Connection refused")
    assert _is_transient_called_process_error(exc) is True


def test_cpe_transient_git_http_500():
    exc = subprocess.CalledProcessError(
        1, "git", stderr="HTTP/1.1 500 Internal Server Error"
    )
    assert _is_transient_called_process_error(exc) is True


def test_cpe_transient_git_fatal_unable_to_access():
    exc = subprocess.CalledProcessError(
        1, "git", stderr="fatal: unable to access 'https://...'"
    )
    assert _is_transient_called_process_error(exc) is True


def test_cpe_transient_git_remote_rejected_internal_server():
    exc = subprocess.CalledProcessError(
        1, "git", stderr="remote rejected: Internal Server"
    )
    assert _is_transient_called_process_error(exc) is True


def test_cpe_fatal_stderr_none():
    exc = subprocess.CalledProcessError(1, "git", stderr=None)
    assert _is_transient_called_process_error(exc) is False


def test_cpe_fatal_stderr_bytes():
    exc = subprocess.CalledProcessError(
        1, "git", stderr=b"remote: Internal Server Error"
    )
    assert _is_transient_called_process_error(exc) is True


def test_cpe_fatal_git_permission_denied():
    exc = subprocess.CalledProcessError(1, "git", stderr="fatal: Permission denied")
    assert _is_transient_called_process_error(exc) is False


def test_cpe_fatal_non_git():
    exc = subprocess.CalledProcessError(1, "ls", stderr="ls: not found")
    assert _is_transient_called_process_error(exc) is False


def test_cpe_fatal_not_called_process_error():
    assert _is_transient_called_process_error(ValueError("nope")) is False


# ---------------------------------------------------------------------------
# classify_stage_error
# ---------------------------------------------------------------------------


def test_classify_transient_direct_httpx():
    assert classify_stage_error(httpx.ConnectError("connection refused")) == "transient"


def test_classify_transient_direct_openai():
    assert (
        classify_stage_error(
            openai.RateLimitError("rate", response=_httpx_response_500, body=None)
        )
        == "transient"
    )


def test_classify_transient_direct_cpe():
    exc = subprocess.CalledProcessError(
        1, "git", stderr="remote: Internal Server Error"
    )
    assert classify_stage_error(exc) == "transient"


def test_classify_fatal_direct():
    assert classify_stage_error(ValueError("boom")) == "fatal"


def test_classify_transient_in_cause_chain():
    exc = ValueError("outer")
    exc.__cause__ = httpx.ConnectError("connection refused")
    assert classify_stage_error(exc) == "transient"


def test_classify_transient_in_context_chain():
    exc = ValueError("outer")
    exc.__context__ = openai.APITimeoutError("timeout")
    assert classify_stage_error(exc) == "transient"


def test_classify_prefers_cause_over_context():
    exc = ValueError("outer")
    cause = httpx.ConnectError("transient cause")
    context = ValueError("fatal context")
    exc.__cause__ = cause
    exc.__context__ = context
    assert classify_stage_error(exc) == "transient"


def test_classify_transient_deep_in_chain():
    # 5-deep chain, transient at level 4 (0-indexed from top)
    e5 = ValueError("level 5")
    e4 = httpx.ConnectError("transient at level 4")
    e3 = ValueError("level 3")
    e2 = ValueError("level 2")
    e1 = ValueError("level 1")
    e3.__cause__ = e4
    e2.__cause__ = e3
    e1.__cause__ = e2
    e4.__cause__ = e5
    # Walk: e1→e2→e3→e4 (transient!) → "transient"
    assert classify_stage_error(e1) == "transient"


def test_classify_fatal_exhausted_chain():
    # 3-deep chain, all ValueError
    e3 = ValueError("level 3")
    e2 = ValueError("level 2")
    e1 = ValueError("level 1")
    e2.__cause__ = e3
    e1.__cause__ = e2
    assert classify_stage_error(e1) == "fatal"


def test_classify_max_chain_walk_guard():
    # 15-deep chain, transient at depth 14 (beyond _MAX_CHAIN_WALK=10)
    node: BaseException = httpx.ConnectError("transient deep")
    for i in range(14):
        wrapper = ValueError(f"layer {i}")
        wrapper.__cause__ = node
        node = wrapper
    # node is now the top of a 15-exception chain.
    # The deepest is ConnectError (depth 14, 0-indexed from top).
    # Walk visits 10 levels (0-9), never reaches ConnectError → "fatal"
    assert classify_stage_error(node) == "fatal"


def test_classify_cycle_detection():
    a = ValueError("a")
    b = ValueError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert classify_stage_error(a) == "fatal"


def test_classify_none_cause_context():
    exc = ValueError("top")
    exc.__cause__ = None
    exc.__context__ = None
    assert classify_stage_error(exc) == "fatal"
