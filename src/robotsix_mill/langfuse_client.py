"""Read-side Langfuse helper: fetch a compact summary of the traces for
a ticket's session (mill sets ``session.id = ticket.id``).

Used by the retrospect stage. Fully graceful: returns ``None`` when
Langfuse isn't configured or the API errors — retrospect then does a
workflow-only review. Ported in spirit from robotsix-project's
``langfuse_client``.
"""

from __future__ import annotations

import base64

from .config import Settings


def fetch_session_summary(settings: Settings, session_id: str) -> str | None:
    """Return a short text summary of the session's traces (count,
    total cost, total latency, per-trace lines), or ``None`` if Langfuse
    is unconfigured / unreachable."""
    if not settings.tracing_enabled:
        return None
    host = (settings.langfuse_base_url or "").rstrip("/")
    auth = base64.b64encode(
        f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}"
        .encode()
    ).decode()
    try:
        import httpx

        with httpx.Client(timeout=20) as c:
            r = c.get(
                f"{host}/api/public/traces",
                params={"sessionId": session_id, "limit": 100},
                headers={"Authorization": f"Basic {auth}"},
            )
        if r.status_code != 200:
            return f"(Langfuse API {r.status_code} — trace data unavailable)"
        traces = r.json().get("data", [])
    except Exception as e:  # noqa: BLE001 — analysis must not fail the stage
        return f"(Langfuse fetch error: {type(e).__name__} — no trace data)"

    if not traces:
        return "(no Langfuse traces found for this session)"

    def num(x):
        try:
            return float(x or 0)
        except (TypeError, ValueError):
            return 0.0

    total_cost = sum(num(t.get("totalCost")) for t in traces)
    total_lat = sum(num(t.get("latency")) for t in traces)
    lines = [
        f"traces={len(traces)}  total_cost=${total_cost:.4f}  "
        f"total_latency={total_lat:.1f}s",
    ]
    for t in traces[:40]:
        lines.append(
            f"- {t.get('name', '?')}  "
            f"${num(t.get('totalCost')):.4f}  "
            f"{num(t.get('latency')):.1f}s  "
            f"obs={len(t.get('observations') or [])}"
        )
    return "\n".join(lines)
