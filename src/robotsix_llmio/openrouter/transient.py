"""OpenRouter-specific transient signatures, layered on the core set."""

from __future__ import annotations

from ..core import retry as _core_retry


def is_openrouter_upstream_error(exc: BaseException) -> bool:
    """Recognise OpenRouter's ``finish_reason='error'`` upstream-failure
    signature.

    When the provider behind OpenRouter errors mid-stream, OpenRouter returns
    a completion with ``finish_reason: "error"``. The OpenAI SDK then raises a
    pydantic ``ValidationError`` because ``"error"`` isn't in its
    ``finish_reason`` literal set. That's an upstream hiccup, not a bug in the
    prompt/schema — a re-run almost always succeeds, so ride it out.

    Matched by the exception type name (``ValidationError``) plus the
    distinctive ``finish_reason`` + ``'error'`` markers, so it does NOT catch
    genuine structured-output validation failures (those don't mention
    ``finish_reason``).
    """
    if type(exc).__name__ != "ValidationError":
        return False
    msg = str(exc)
    return "finish_reason" in msg and "'error'" in msg


def is_openrouter_transient(exc: BaseException) -> bool:
    """Core transient set OR the OpenRouter upstream-error signature, walking
    the cause/context chain for the latter."""
    if _core_retry.is_transient(exc):
        return True
    for cur in _core_retry._walk_cause_chain(exc):
        if is_openrouter_upstream_error(cur):
            return True
    return False
