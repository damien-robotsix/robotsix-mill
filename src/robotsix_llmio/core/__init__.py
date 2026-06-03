"""Provider-agnostic LLM I/O base."""

from __future__ import annotations

from .agent import AgentHandle, build_agent
from .cost_log import CostLogSource, CostRecord, CostWindow, LoggedCost
from .http import timeout_http_client
from .langfuse_cost import LangfuseCostLogSource
from .provider import LLMProvider, Tier
from .retry import (
    call_with_retry,
    call_with_retry_and_fallback,
    is_rate_limited,
    is_transient,
)
from .tracing import (
    TraceSpan,
    current_session,
    flush_tracing,
    get_recording_span,
    get_tracer,
    install_signal_handlers,
    langfuse_project,
    langfuse_session,
    langfuse_trace_url,
    make_session_id,
    setup_langfuse_tracing,
    start_span,
    start_trace,
)

__all__ = [
    "AgentHandle",
    "build_agent",
    "CostWindow",
    "CostRecord",
    "LoggedCost",
    "CostLogSource",
    "LangfuseCostLogSource",
    "timeout_http_client",
    "LLMProvider",
    "Tier",
    "call_with_retry",
    "call_with_retry_and_fallback",
    "is_rate_limited",
    "is_transient",
    "setup_langfuse_tracing",
    "langfuse_session",
    "langfuse_project",
    "start_trace",
    "TraceSpan",
    "current_session",
    "make_session_id",
    "langfuse_trace_url",
    "install_signal_handlers",
    "flush_tracing",
    "get_recording_span",
    "get_tracer",
    "start_span",
]
