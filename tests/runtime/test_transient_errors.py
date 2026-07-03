"""Unit tests for robotsix_mill.runtime.transient_errors.

Pure unit tests — no I/O, no async, no fixtures beyond ``monkeypatch``.
Every function in the module gets direct test coverage.
"""

import subprocess
from unittest.mock import Mock

import httpx
import openai

import pytest

from robotsix_mill.runtime.transient_errors import (
    _is_transient_called_process_error,
    _is_transient_httpx,
    _is_transient_openai,
    classify_stage_error,
    reraise_if_transient,
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


def test_cpe_transient_git_authentication_failed():
    exc = subprocess.CalledProcessError(1, "git", stderr="fatal: Authentication failed")
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


# ---------------------------------------------------------------------------
# DeepSeek thinking-mode reasoning round-trip 400 — the special-case detector
# was REMOVED (OpenRouter no longer raises this 400 when reasoning is stripped
# from a tool-call turn, so robotsix-llmio dropped the detector). A 400 of any
# shape is now classified fatal. See project-deepseek-pin-reasoning-blocker.
# ---------------------------------------------------------------------------


def _reasoning_400():
    from pydantic_ai.exceptions import ModelHTTPError

    return ModelHTTPError(
        400,
        "deepseek/deepseek-v4-pro",
        {
            "error": {
                "message": (
                    "The reasoning_content in the thinking mode must be "
                    "passed back to the API."
                )
            }
        },
    )


def test_classify_reasoning_400_is_now_fatal_direct():
    # Detector removed → a reasoning-shaped 400 is just a fatal 400.
    assert classify_stage_error(_reasoning_400()) == "fatal"


def test_classify_reasoning_400_is_now_fatal_in_cause_chain():
    outer = RuntimeError("agent run failed")
    outer.__cause__ = _reasoning_400()
    assert classify_stage_error(outer) == "fatal"


def test_classify_fatal_plain_400():
    from pydantic_ai.exceptions import ModelHTTPError

    plain = ModelHTTPError(400, "x", {"error": {"message": "bad request"}})
    assert classify_stage_error(plain) == "fatal"


def test_classify_claude_sdk_degenerate_success_is_transient():
    """The ``error result: success`` message is transient at the stage level."""
    assert (
        classify_stage_error(Exception("Claude Code returned an error result: success"))
        == "transient"
    )


def test_classify_claude_sdk_degenerate_success_in_cause_chain():
    """The degenerate result in a cause chain is still transient."""
    inner = Exception("Claude Code returned an error result: success")
    outer = RuntimeError("agent run failed")
    outer.__cause__ = inner
    assert classify_stage_error(outer) == "transient"


# ---------------------------------------------------------------------------
# _is_transient_message — message-string fallback for transient patterns
# not caught by exception-type checks (e.g. pydantic-ai's
# UnexpectedModelBehavior wrapping openrouter errors).
# ---------------------------------------------------------------------------


def test_classify_transient_invalid_response_from_openrouter():
    """'Invalid response from openrouter' in str(exc) → transient."""
    exc = Exception("Invalid response from openrouter chat completions endpoint")
    assert classify_stage_error(exc) == "transient"


def test_classify_transient_invalid_response_from_openrouter_lowercase():
    """Case-insensitive match for the openrouter invalid-response pattern."""
    exc = Exception("invalid response from openrouter: expected JSON data")
    assert classify_stage_error(exc) == "transient"


def test_classify_transient_invalid_response_in_cause_chain():
    """The openrouter pattern is transient even when nested in a cause chain."""
    inner = Exception("Invalid response from openrouter chat completions endpoint")
    outer = RuntimeError("agent run failed")
    outer.__cause__ = inner
    assert classify_stage_error(outer) == "transient"


def test_classify_transient_exceeded_max_output_retries():
    """'Exceeded max output retries' in str(exc) → transient."""
    exc = Exception("Exceeded maximum output retries (5)")
    assert classify_stage_error(exc) == "transient"


def test_classify_transient_exceeded_max_output_retries_lowercase():
    """Case-insensitive match for the output-retries pattern."""
    exc = Exception("exceeded max output retries (3)")
    assert classify_stage_error(exc) == "transient"


# ---------------------------------------------------------------------------
# reraise_if_transient — LLM stages (review/refine/retrospect) call this so a
# transient model error gets the worker's stage-retry instead of a hard BLOCK.
# ---------------------------------------------------------------------------


def test_reraise_if_transient_returns_on_reasoning_400():
    # Detector removed → a reasoning-shaped 400 is fatal, so reraise_if_transient
    # returns (the caller blocks) rather than re-raising for a stage-retry.
    assert reraise_if_transient(_reasoning_400()) is None


def test_reraise_if_transient_reraises_httpx_timeout():
    exc = httpx.ReadTimeout("slow")
    with pytest.raises(httpx.ReadTimeout):
        reraise_if_transient(exc)


def test_reraise_if_transient_returns_on_fatal():
    # Fatal errors return (None) so the caller blocks as before.
    assert reraise_if_transient(ValueError("boom")) is None


def test_reraise_if_transient_returns_on_plain_400():
    from pydantic_ai.exceptions import ModelHTTPError

    plain = ModelHTTPError(400, "x", {"error": {"message": "bad request"}})
    assert reraise_if_transient(plain) is None


# --- workspace-gone git errors → transient (auto-retry → re-clone) ----------


def test_workspace_gone_not_a_git_repo_is_transient():
    import subprocess

    from robotsix_mill.runtime.transient_errors import classify_stage_error

    e = subprocess.CalledProcessError(
        128,
        ["git", "-C", "/data/ws/repo", "status", "--porcelain"],
        stderr="fatal: not a git repository (or any of the parent directories): .git",
    )
    assert classify_stage_error(e) == "transient"


def test_workspace_gone_missing_dir_is_transient():
    import subprocess

    from robotsix_mill.runtime.transient_errors import classify_stage_error

    e = subprocess.CalledProcessError(
        128,
        ["git", "-C", "/data/ws/repo", "status"],
        stderr="fatal: cannot change to '/data/ws/repo': No such file or directory",
    )
    assert classify_stage_error(e) == "transient"


def test_real_git_error_stays_fatal():
    import subprocess

    from robotsix_mill.runtime.transient_errors import classify_stage_error

    e = subprocess.CalledProcessError(
        1, ["git", "status"], stderr="error: pathspec 'x' did not match any file(s)"
    )
    assert classify_stage_error(e) == "fatal"


# ---------------------------------------------------------------------------
# Network-outage detection (is_network_down_error / network_available)
# ---------------------------------------------------------------------------


def test_is_network_down_error_git_dns_failure():
    from robotsix_mill.runtime.transient_errors import (
        classify_stage_error,
        is_network_down_error,
    )

    e = subprocess.CalledProcessError(
        128,
        "git",
        stderr=(
            "fatal: unable to access 'https://github.com/x/y/': "
            "Could not resolve host: github.com"
        ),
    )
    assert is_network_down_error(e)
    # Still transient for the normal classifier too.
    assert classify_stage_error(e) == "transient"


def test_is_network_down_error_gaierror_in_cause_chain():
    import socket

    from robotsix_mill.runtime.transient_errors import is_network_down_error

    inner = socket.gaierror(-3, "Temporary failure in name resolution")
    outer = RuntimeError("wrapped")
    outer.__cause__ = inner
    assert is_network_down_error(outer)


def test_is_network_down_error_rejects_endpoint_errors():
    from robotsix_mill.runtime.transient_errors import is_network_down_error

    e = subprocess.CalledProcessError(
        1, "git", stderr="The requested URL returned error: 503"
    )
    assert not is_network_down_error(e)
    assert not is_network_down_error(RuntimeError("plain failure"))


def test_network_available_probes_and_caches(monkeypatch):
    import robotsix_mill.runtime.transient_errors as te

    monkeypatch.setattr(te, "_probe_cache", {"at": float("-inf"), "ok": True})
    calls = {"n": 0}

    def fake_getaddrinfo(host, port):
        calls["n"] += 1
        raise OSError("no dns")

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)
    assert te.network_available("github.com", cache_seconds=300.0) is False
    assert te.network_available("github.com", cache_seconds=300.0) is False
    assert calls["n"] == 1, "second call within the cache window must not probe"


def test_network_available_true_when_host_resolves(monkeypatch):
    import robotsix_mill.runtime.transient_errors as te

    monkeypatch.setattr(te, "_probe_cache", {"at": float("-inf"), "ok": False})
    monkeypatch.setattr("socket.getaddrinfo", lambda host, port: [("ok",)])
    assert te.network_available("github.com", cache_seconds=300.0) is True
