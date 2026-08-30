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
    """The ``error result: success`` message is NOT transient at the stage level
    — it is deterministic and handled by the refine runner instead."""
    assert (
        classify_stage_error(Exception("Claude Code returned an error result: success"))
        == "fatal"
    )


def test_classify_claude_sdk_degenerate_success_in_cause_chain():
    """The degenerate result in a cause chain is NOT transient."""
    inner = Exception("Claude Code returned an error result: success")
    outer = RuntimeError("agent run failed")
    outer.__cause__ = inner
    assert classify_stage_error(outer) == "fatal"


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


def test_classify_transient_sqlite_database_locked():
    """SQLite lock contention on the mill's own DB → transient."""
    exc = Exception(
        "(sqlite3.OperationalError) database is locked "
        "[SQL: INSERT INTO comment (ticket_id, body, author, parent_id, "
        "closed_at, created_at) VALUES (?, ?, ?, ?, ?, ?)]"
    )
    assert classify_stage_error(exc) == "transient"


def test_classify_transient_sqlite_database_locked_in_cause_chain():
    """The db-locked pattern is transient even when nested in a cause chain."""
    inner = Exception("(sqlite3.OperationalError) database is locked")
    outer = Exception("agent run failed")
    outer.__cause__ = inner
    assert classify_stage_error(outer) == "transient"


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


# ---------------------------------------------------------------------------
# SandboxError transient classification — docker "unexpected EOF" and
# similar infra failures must be transient so the stage retries instead
# of hard-BLOCKing.  See ticket 20260715T070655Z (implement stage stall
# hardening).
# ---------------------------------------------------------------------------


def test_sandbox_error_is_transient():
    """SandboxError (docker unexpected EOF, daemon errors) is transient."""
    from robotsix_mill.runtime.transient_errors import classify_stage_error
    from robotsix_mill.sandbox import SandboxError

    exc = SandboxError("docker run failed: unexpected EOF")
    assert classify_stage_error(exc) == "transient"


def test_sandbox_error_transient_in_cause_chain():
    """SandboxError nested in a cause chain is still transient."""
    from robotsix_mill.runtime.transient_errors import classify_stage_error
    from robotsix_mill.sandbox import SandboxError

    inner = SandboxError("docker daemon error")
    outer = RuntimeError("agent run failed")
    outer.__cause__ = inner
    assert classify_stage_error(outer) == "transient"


# ---------------------------------------------------------------------------
# Model-outage detection (is_model_unavailable_error)
# ---------------------------------------------------------------------------


def test_is_model_unavailable_error_503_body_match():
    """httpx 503 with "model unavailable" → model outage detected."""
    from robotsix_mill.runtime.transient_errors import is_model_unavailable_error

    exc = httpx.HTTPStatusError(
        "boom",
        request=_httpx_request,
        response=_throttle_response(
            503, text='{"error":{"message":"model currently unavailable"}}'
        ),
    )
    assert is_model_unavailable_error(exc)


def test_is_model_unavailable_error_no_healthy_endpoint():
    """httpx 503 with "no healthy endpoint" → model outage detected."""
    from robotsix_mill.runtime.transient_errors import is_model_unavailable_error

    exc = httpx.HTTPStatusError(
        "boom",
        request=_httpx_request,
        response=_throttle_response(
            503, text='{"error":{"message":"no healthy endpoint available"}}'
        ),
    )
    assert is_model_unavailable_error(exc)


def test_is_model_unavailable_error_overloaded():
    """String "provider overloaded" → model outage detected."""
    from robotsix_mill.runtime.transient_errors import is_model_unavailable_error

    exc = RuntimeError("provider is currently overloaded, try again later")
    assert is_model_unavailable_error(exc)


def test_is_model_unavailable_error_tier_cooldown_exhausted():
    """llmio "tier in cooldown, fallback depth exhausted" → model outage."""
    from robotsix_mill.runtime.transient_errors import (
        classify_stage_error,
        is_model_unavailable_error,
    )

    exc = RuntimeError(
        "scope triage: level2 is in cooldown and fallback depth (2) exhausted"
    )
    assert is_model_unavailable_error(exc)
    assert classify_stage_error(exc) == "transient"


def test_is_model_unavailable_error_rejects_generic_503():
    """A bare 503 without outage signal is NOT a model outage."""
    from robotsix_mill.runtime.transient_errors import is_model_unavailable_error

    exc = httpx.HTTPStatusError(
        "boom",
        request=_httpx_request,
        response=_throttle_response(503, text="Service Unavailable"),
    )
    assert not is_model_unavailable_error(exc)


def test_is_model_unavailable_error_rejects_unrelated():
    """Plain errors are not model outages."""
    from robotsix_mill.runtime.transient_errors import is_model_unavailable_error

    assert not is_model_unavailable_error(RuntimeError("plain failure"))
    assert not is_model_unavailable_error(ValueError("something broke"))


def test_is_model_unavailable_error_in_cause_chain():
    """Outage signal in the cause chain still matches."""
    from robotsix_mill.runtime.transient_errors import is_model_unavailable_error

    inner = RuntimeError("the model deepseek-chat is currently unavailable")
    outer = RuntimeError("wrapped")
    outer.__cause__ = inner
    assert is_model_unavailable_error(outer)


def test_model_unavailable_classifies_transient():
    """Model-unavailable errors are classified as transient (park branch lives there)."""
    from robotsix_mill.runtime.transient_errors import classify_stage_error

    exc = httpx.HTTPStatusError(
        "boom",
        request=_httpx_request,
        response=_throttle_response(
            503, text='{"error":{"message":"model unavailable"}}'
        ),
    )
    assert classify_stage_error(exc) == "transient"


def test_model_outage_marker_is_stable():
    """MODEL_OUTAGE_MARKER is a plain string constant for use in notes."""
    from robotsix_mill.runtime.transient_errors import MODEL_OUTAGE_MARKER

    assert MODEL_OUTAGE_MARKER == "model_outage"
    assert isinstance(MODEL_OUTAGE_MARKER, str)


# ---------------------------------------------------------------------------
# Disk-exhaustion detection (is_disk_full_error / disk_space_available)
# ---------------------------------------------------------------------------


def test_is_disk_full_error_enospc_oserror():
    """A bare ENOSPC OSError — the shape mill's own writes produce."""
    import errno

    from robotsix_mill.runtime.transient_errors import is_disk_full_error

    e = OSError(errno.ENOSPC, "No space left on device", "/data/board/ws")
    assert is_disk_full_error(e)


def test_is_disk_full_error_git_clone_stderr():
    """A clone that died on a full volume reaches us as a
    CalledProcessError; without the stderr match its block note reads
    'Fatal: CalledProcessError' and names git, not the disk."""
    import subprocess

    from robotsix_mill.runtime.transient_errors import is_disk_full_error

    e = subprocess.CalledProcessError(
        128,
        ["git", "clone", "--quiet", "https://github.com/x/y", "/data/b/ws/repo"],
        stderr="fatal: could not create work tree dir: No space left on device",
    )
    assert is_disk_full_error(e)


def test_is_disk_full_error_in_cause_chain():
    """ENOSPC wrapped by a higher-level error is still found."""
    import errno

    from robotsix_mill.runtime.transient_errors import is_disk_full_error

    inner = OSError(errno.ENOSPC, "No space left on device")
    outer = RuntimeError("workspace setup failed")
    outer.__cause__ = inner
    assert is_disk_full_error(outer)


def test_is_disk_full_error_rejects_unrelated_failures():
    """Ordinary failures must not be mistaken for disk exhaustion —
    parking a genuine defect would hide it forever."""
    import errno
    import subprocess

    from robotsix_mill.runtime.transient_errors import is_disk_full_error

    assert not is_disk_full_error(RuntimeError("assertion failed"))
    assert not is_disk_full_error(OSError(errno.EACCES, "Permission denied"))
    assert not is_disk_full_error(
        subprocess.CalledProcessError(1, "git", stderr="fatal: not a git repository")
    )


def test_disk_full_error_classifies_transient():
    """ENOSPC must reach the transient path — the disk-full park lives
    inside it, and 'fatal' is what made each one a manual resume."""
    import errno

    from robotsix_mill.runtime.transient_errors import classify_stage_error

    e = OSError(errno.ENOSPC, "No space left on device")
    assert classify_stage_error(e) == "transient"


def test_disk_space_available_reads_statvfs(monkeypatch, tmp_path):
    """The floor is compared against real available blocks."""
    import os

    from robotsix_mill.runtime.transient_errors import disk_space_available

    class FakeStat:
        f_frsize = 4096

        def __init__(self, avail):
            self.f_bavail = avail

    # 100 MB free against a 50 MB floor -> fine; against 500 MB -> not.
    monkeypatch.setattr(os, "statvfs", lambda p: FakeStat(100 * 1024 * 1024 // 4096))
    assert disk_space_available(tmp_path, 50)
    assert not disk_space_available(tmp_path, 500)


def test_disk_space_available_floor_zero_disables(tmp_path):
    """A 0 floor turns the check off entirely."""
    from robotsix_mill.runtime.transient_errors import disk_space_available

    assert disk_space_available(tmp_path, 0)


def test_disk_space_available_unreadable_path_is_permissive(monkeypatch, tmp_path):
    """An unstattable mount must not park the whole fleet."""
    import os

    from robotsix_mill.runtime.transient_errors import disk_space_available

    def boom(_p):
        raise OSError("nope")

    monkeypatch.setattr(os, "statvfs", boom)
    assert disk_space_available(tmp_path, 5_000)


def test_first_full_path_catches_a_second_filesystem(monkeypatch):
    """The regression: the data volume has room but the overlay does not.

    Observed live 2026-08-07 — a rebase failed three times with ENOSPC on
    every ``run_command`` while the data volume reported 146 GB free,
    because the container root (the sandbox's Docker overlay) was at 80%.
    Checking only the data volume waved the ticket into the wall.
    """
    import os

    from robotsix_mill.runtime.transient_errors import first_full_path

    class FakeStat:
        f_frsize = 4096

        def __init__(self, avail_mb):
            self.f_bavail = avail_mb * 1024 * 1024 // 4096

    free = {"/data": 146_000, "/": 100}
    monkeypatch.setattr(os, "statvfs", lambda p: FakeStat(free[str(p)]))

    # The data volume alone looks fine — this is exactly the blind spot.
    assert first_full_path(["/data"], 5_120) is None
    # Including the container root catches it, and names the culprit.
    assert first_full_path(["/data", "/"], 5_120) == "/"


def test_first_full_path_returns_none_when_all_have_room(monkeypatch):
    import os

    from robotsix_mill.runtime.transient_errors import first_full_path

    class FakeStat:
        f_frsize = 4096
        f_bavail = 50_000 * 1024 * 1024 // 4096

    monkeypatch.setattr(os, "statvfs", lambda p: FakeStat())
    assert first_full_path(["/data", "/", "/tmp"], 5_120) is None


def test_first_full_path_reports_the_first_offender(monkeypatch):
    """Order matters: the caller names this path in the park note."""
    import os

    from robotsix_mill.runtime.transient_errors import first_full_path

    class FakeStat:
        f_frsize = 4096

        def __init__(self, avail_mb):
            self.f_bavail = avail_mb * 1024 * 1024 // 4096

    monkeypatch.setattr(os, "statvfs", lambda p: FakeStat(10))
    assert first_full_path(["/data", "/"], 5_120) == "/data"


def test_first_full_path_skips_unstattable_paths(monkeypatch):
    """A path that cannot be stat'ed degrades to 'has room' rather than
    parking the fleet — same fail-open contract as disk_space_available,
    so listing a path absent from some deployment is harmless."""
    import os

    from robotsix_mill.runtime.transient_errors import first_full_path

    def boom(p):
        raise OSError("no such mount")

    monkeypatch.setattr(os, "statvfs", boom)
    assert first_full_path(["/nope", "/also-nope"], 5_120) is None


def test_first_full_path_empty_list_is_no_constraint():
    from robotsix_mill.runtime.transient_errors import first_full_path

    assert first_full_path([], 5_120) is None


# ---------------------------------------------------------------------------
# _is_github_rate_limited — GitHub answers a throttled caller with 403/429,
# not 5xx, so these must not fall through to "fatal" and block a ticket.
# ---------------------------------------------------------------------------


def _throttle_response(status, headers=None, text=""):
    """Build a minimal response double with real str headers and body."""
    return Mock(status_code=status, headers=dict(headers or {}), text=text)


def test_github_403_exhausted_quota_is_transient():
    exc = httpx.HTTPStatusError(
        "boom",
        request=_httpx_request,
        response=_throttle_response(403, {"x-ratelimit-remaining": "0"}),
    )
    assert _is_transient_httpx(exc) is True


def test_github_403_with_retry_after_is_transient():
    exc = httpx.HTTPStatusError(
        "boom",
        request=_httpx_request,
        response=_throttle_response(403, {"retry-after": "60"}),
    )
    assert _is_transient_httpx(exc) is True


def test_github_403_secondary_rate_limit_body_is_transient():
    exc = httpx.HTTPStatusError(
        "boom",
        request=_httpx_request,
        response=_throttle_response(
            403, text='{"message":"You have exceeded a secondary rate limit"}'
        ),
    )
    assert _is_transient_httpx(exc) is True


def test_github_429_with_retry_after_is_transient():
    exc = httpx.HTTPStatusError(
        "boom",
        request=_httpx_request,
        response=_throttle_response(429, {"retry-after": "30"}),
    )
    assert _is_transient_httpx(exc) is True


def test_github_403_permission_denied_stays_fatal():
    """A real refusal must keep blocking — retrying it forever helps nobody."""
    exc = httpx.HTTPStatusError(
        "boom",
        request=_httpx_request,
        response=_throttle_response(
            403, text='{"message":"Resource not accessible by integration"}'
        ),
    )
    assert _is_transient_httpx(exc) is False


def test_github_403_with_remaining_quota_stays_fatal():
    exc = httpx.HTTPStatusError(
        "boom",
        request=_httpx_request,
        response=_throttle_response(403, {"x-ratelimit-remaining": "4321"}),
    )
    assert _is_transient_httpx(exc) is False


def test_github_throttle_classified_transient_end_to_end():
    """classify_stage_error is what decides retry-vs-block for a stage."""
    exc = httpx.HTTPStatusError(
        "boom",
        request=_httpx_request,
        response=_throttle_response(403, {"x-ratelimit-remaining": "0"}),
    )
    assert classify_stage_error(exc) == "transient"


# --- is_provider_failure -----------------------------------------------------

_LIVE_DUAL_FAILURE = (
    "output retries exhausted on primary + fallback models: primary=Model token "
    "limit (32768) exceeded before any response was generated. Increase the "
    "`max_tokens` model setting, or simplify the prompt, fallback=status_code: "
    "400, model_name: deepseek/deepseek-v4-flash-latest, body: {'message': "
    "'deepseek/deepseek-v4-flash-latest is not a valid model ID', 'code': 400}"
)


@pytest.mark.parametrize(
    "message",
    [
        _LIVE_DUAL_FAILURE,
        (
            "status_code: 400, model_name: deepseek/deepseek-v4-flash-latest, body: "
            "{'message': 'deepseek/deepseek-v4-flash-latest is not a valid model ID'}"
        ),
        "Model token limit (32768) exceeded before any response was generated.",
        "output retries exhausted on primary + fallback models: primary=x, fallback=y",
    ],
)
def test_is_provider_failure_matches_live_model_failures(message):
    from robotsix_mill.runtime.transient_errors import is_provider_failure

    assert is_provider_failure(RuntimeError(message))


@pytest.mark.parametrize(
    "message",
    [
        "Tool 'verify_diff' exceeded max retries count of 2",
        "summary verification failed after retry: test_utils.py",
        "boom",
    ],
)
def test_is_provider_failure_rejects_agent_behaviour_failures(message):
    """A tool-validation loop or a hallucinated summary IS about this spec."""
    from robotsix_mill.runtime.transient_errors import is_provider_failure

    assert not is_provider_failure(RuntimeError(message))


def test_is_provider_failure_walks_cause_chain():
    from robotsix_mill.runtime.transient_errors import is_provider_failure

    inner = RuntimeError("deepseek/x is not a valid model ID")
    outer = RuntimeError("agent error")
    outer.__cause__ = inner
    assert is_provider_failure(outer)


def test_invalid_model_id_is_fatal_not_transient():
    """A bad baked model id does not fix itself between retries: it blocks
    (without a fingerprint — see implement stage), it is not retried."""
    assert (
        classify_stage_error(RuntimeError("deepseek/x is not a valid model ID"))
        == "fatal"
    )


# --- Claude usage exhaustion → park, no fingerprint, no paid fallback -------


class _FakeUsageExhausted(Exception):
    pass


_FakeUsageExhausted.__name__ = "ClaudeSDKUsageExhaustedError"


@pytest.mark.parametrize(
    "message",
    [
        "You've hit your session limit · resets 9:20am (UTC)",
        "You're out of usage credits",
        "You've hit your limit · resets 8pm (UTC)",
        "Usage limit reached for this tier",
    ],
)
def test_claude_usage_exhaustion_is_parkable_provider_failure(message):
    from robotsix_mill.runtime.transient_errors import (
        classify_stage_error,
        is_claude_usage_exhausted,
        is_model_unavailable_error,
        is_provider_failure,
    )

    exc = RuntimeError(message)
    assert is_claude_usage_exhausted(exc)
    # routes into the worker's model-outage PARK branch …
    assert classify_stage_error(exc) == "transient"
    assert is_model_unavailable_error(exc)
    # … and the implement stage must not record a spec fingerprint for it
    assert is_provider_failure(exc)


def test_claude_usage_exhaustion_matches_llmio_class_through_cause_chain():
    from robotsix_mill.runtime.transient_errors import is_claude_usage_exhausted

    inner = _FakeUsageExhausted("opaque text")
    outer = RuntimeError("agent run failed")
    outer.__cause__ = inner
    assert is_claude_usage_exhausted(outer)
    assert not is_claude_usage_exhausted(RuntimeError("tests failed: 3 errors"))


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("You've hit your session limit · resets 9:20am (UTC)", (9, 20)),
        ("You've hit your limit · resets 8pm (UTC)", (20, 0)),
        ("hit your session limit · resets 12am (UTC)", (0, 0)),
        ("hit your session limit · resets 12:05pm (UTC)", (12, 5)),
    ],
)
def test_claude_usage_reset_at_parses_next_reset(message, expected):
    from datetime import UTC, datetime

    from robotsix_mill.runtime.transient_errors import claude_usage_reset_at

    now = datetime(2026, 8, 29, 8, 45, tzinfo=UTC)
    got = claude_usage_reset_at(RuntimeError(message), now=now)
    assert got is not None
    assert (got.hour, got.minute) == expected
    assert got > now
    assert got.tzinfo is UTC


def test_claude_usage_reset_at_rolls_to_tomorrow_when_already_past():
    from datetime import UTC, datetime

    from robotsix_mill.runtime.transient_errors import claude_usage_reset_at

    now = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)
    got = claude_usage_reset_at(
        RuntimeError("hit your session limit · resets 9:20am (UTC)"), now=now
    )
    assert got == datetime(2026, 8, 30, 9, 20, tzinfo=UTC)


def test_claude_usage_reset_at_none_without_hint():
    from robotsix_mill.runtime.transient_errors import claude_usage_reset_at

    assert claude_usage_reset_at(RuntimeError("You're out of usage credits")) is None
