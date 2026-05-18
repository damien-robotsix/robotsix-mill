"""Read-side Langfuse helper: fetch a compact summary of the traces for
a ticket's session (mill sets ``session.id = ticket.id``), and list all
traces in a time window for health checks.

Used by the retrospect stage and the trace-health runner. Fully
graceful: returns ``None`` / ``[]`` when Langfuse isn't configured or
the API errors.
"""

from __future__ import annotations

import base64
import logging

from .config import Settings

log = logging.getLogger("robotsix_mill.langfuse_client")


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


def list_all_traces_since(
    settings: Settings, from_timestamp: str
) -> list[dict]:
    """Return every trace created at or after *from_timestamp* by
    paginating the Langfuse public API.

    Returns an empty list (never crashes) when Langfuse is unconfigured
    or any HTTP / JSON error occurs — the caller must treat ``[]`` as
    "no data available."
    """
    if not settings.tracing_enabled:
        return []
    host = (settings.langfuse_base_url or "").rstrip("/")
    auth = base64.b64encode(
        f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}"
        .encode()
    ).decode()
    try:
        import httpx

        all_traces: list[dict] = []
        page = 1
        with httpx.Client(timeout=30) as c:
            while True:
                r = c.get(
                    f"{host}/api/public/traces",
                    params={
                        "fromTimestamp": from_timestamp,
                        "limit": 50,
                        "page": page,
                    },
                    headers={"Authorization": f"Basic {auth}"},
                )
                if r.status_code != 200:
                    log.warning(
                        "Langfuse trace list returned %d on page %d",
                        r.status_code,
                        page,
                    )
                    return []
                body = r.json()
                data = body.get("data", [])
                all_traces.extend(data)
                meta = body.get("meta", {})
                total_pages = meta.get("totalPages", 1)
                if page >= total_pages:
                    break
                page += 1
        return all_traces
    except Exception:  # noqa: BLE001 — never crash the caller
        log.exception("failed to list Langfuse traces since %s", from_timestamp)
        return []
