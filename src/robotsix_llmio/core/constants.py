"""Baked, non-overridable LLM-I/O parameters.

These are deliberately module constants, not a config object: the whole
point of this library is that a consumer chooses only a provider + tier and
inherits a known-working parameter set. Values mirror the production defaults
proven in robotsix-mill. If a real application later needs to override one,
that override is added explicitly on the derived provider — not exposed as a
general knob here.
"""

from __future__ import annotations

# HTTP — hard per-request timeout so a hung/glacial provider connection raises
# instead of blocking forever. The capable tier routinely runs 60-190s.
MODEL_REQUEST_TIMEOUT: float = 900.0
CONNECT_TIMEOUT: float = 15.0

# Claude Agent SDK — hard per-call wall-clock cap on a single ``query()`` (the
# whole agent loop: subprocess spawn + every turn). The SDK has no equivalent of
# the HTTP timeout above; a stalled CLI subprocess (e.g. startup contention when
# many runs spawn at once) otherwise blocks until the SDK's own ~2h backstop,
# turning one stuck run into a multi-hour hang. Capping it here makes a stall
# fail fast as a ``ClaudeSDKQueryTimeout`` that the bounded retry treats as
# transient — so the work re-runs in minutes instead of hanging. Generous enough
# that a genuine multi-turn tool loop (max_turns=100, ~minutes) doesn't trip it.
SDK_QUERY_TIMEOUT: float = 1200.0  # 20 minutes

# Transient retry (429 / 5xx / timeout / malformed-JSON / upstream-error):
# short exponential backoff with jitter.
TRANSIENT_RETRIES: int = 4
TRANSIENT_BACKOFF_BASE: float = 2.0
TRANSIENT_BACKOFF_CAP: float = 30.0

# Rate-limit (UsageLimitExceeded) — longer, rate-limit-aware schedule.
RATE_LIMIT_BACKOFF_BASE: float = 30.0
RATE_LIMIT_BACKOFF_CAP: float = 120.0
