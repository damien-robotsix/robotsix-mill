"""Provider-agnostic LLM I/O base."""

from __future__ import annotations

from .agent import AgentHandle, build_agent
from .cost_log import CostLogSource, CostRecord, CostWindow, LoggedCost
from .http import timeout_http_client
from .langfuse_cost import LangfuseCostLogSource
from .provider import LLMProvider, Tier
from .provider_cost import (
    DEFAULT_TOLERANCE,
    Discrepancy,
    ProviderCost,
    ProviderCostSource,
    reconcile,
)
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
    "DEFAULT_TOLERANCE",
    "AgentHandle",
    "CostLogSource",
    "CostRecord",
    "CostWindow",
    "Discrepancy",
    "LLMProvider",
    "LangfuseCostLogSource",
    "LoggedCost",
    "ProviderCost",
    "ProviderCostSource",
    "Tier",
    "TraceSpan",
    "build_agent",
    "call_with_retry",
    "call_with_retry_and_fallback",
    "current_session",
    "flush_tracing",
    "get_recording_span",
    "get_tracer",
    "install_signal_handlers",
    "is_rate_limited",
    "is_transient",
    "langfuse_project",
    "langfuse_session",
    "langfuse_trace_url",
    "make_session_id",
    "reconcile",
    "setup_langfuse_tracing",
    "start_span",
    "start_trace",
    "timeout_http_client",
]
