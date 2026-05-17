"""Optional Langfuse tracing.

A pure no-op unless all three ``LANGFUSE_*`` vars are set, and the
``langfuse`` package (the ``[tracing]`` extra) is importable. Stages
wrap their work in ``with span("stage.refine", ticket=id): ...`` and pay
nothing when tracing is off.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext

from ..config import Settings

_client = None  # lazily-created Langfuse client, or None


def init(settings: Settings) -> None:
    global _client
    if not settings.tracing_enabled:
        return
    try:
        from langfuse import Langfuse
    except ImportError:
        # tracing extra not installed — stay silent and disabled
        return
    _client = Langfuse(
        host=settings.langfuse_base_url,
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
    )


@contextmanager
def span(name: str, **attrs):
    if _client is None:
        with nullcontext():
            yield None
        return
    with _client.start_as_current_span(name=name) as s:
        if attrs:
            s.update(metadata=attrs)
        yield s
