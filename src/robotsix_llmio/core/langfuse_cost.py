"""Langfuse implementation of the neutral :class:`CostLogSource` read seam.

The **only** module that knows about the Langfuse REST API on the read path
(the counterpart to the OTLP write export in :mod:`robotsix_llmio.core.tracing`).
Self-contained: depends only on ``httpx`` and the public Langfuse REST API — no
Langfuse SDK, no ``tracing`` extra.

``LangfuseCostLogSource`` reads logged trace cost back over a time window via
``GET /api/public/traces``, paging through all results and aggregating
trace-level ``totalCost`` into a :class:`LoggedCost`. It bakes no credentials:
the consumer always constructs it with explicit keys.
"""

from __future__ import annotations

import base64
from datetime import datetime
from typing import Any

import httpx

from .cost_log import CostRecord, CostWindow, LoggedCost

_DEFAULT_BASE_URL = "https://cloud.langfuse.com"
_PAGE_LIMIT = 100
_TIMEOUT = 20


class LangfuseCostLogSource:
    """Read logged cost back from Langfuse via its public REST API.

    Implements :class:`~robotsix_llmio.core.cost_log.CostLogSource`. Credentials
    are always passed in explicitly — the adapter reads no ``LANGFUSE_*`` env
    vars (env defaulting, if any, belongs to the consumer).
    """

    def __init__(
        self,
        *,
        public_key: str,
        secret_key: str,
        base_url: str | None = None,
    ) -> None:
        self._public_key = public_key
        self._secret_key = secret_key
        self._base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")

    def _auth_header(self) -> str:
        """Build the ``Basic <base64(public:secret)>`` Authorization header."""
        token = base64.b64encode(
            f"{self._public_key}:{self._secret_key}".encode()
        ).decode()
        return f"Basic {token}"

    def fetch_logged_cost(self, window: CostWindow) -> LoggedCost:
        """Fetch and aggregate logged trace cost over *window*.

        Pages through ``/api/public/traces`` (1-based ``page`` + ``limit``)
        until a page returns no data (or the response's ``meta.totalPages`` is
        reached), then sums trace-level ``totalCost`` and builds a
        :class:`CostRecord` per trace. Raises ``RuntimeError`` on any non-2xx
        response rather than silently returning zero.
        """
        url = f"{self._base_url}/api/public/traces"
        headers = {"Authorization": self._auth_header()}
        base_params: dict[str, Any] = {
            "fromTimestamp": window.start.isoformat(),
            "toTimestamp": window.end.isoformat(),
            "limit": _PAGE_LIMIT,
        }

        all_traces: list[dict[str, Any]] = []
        with httpx.Client(timeout=_TIMEOUT) as client:
            page = 1
            while True:
                resp = client.get(
                    url,
                    params={**base_params, "page": page},
                    headers=headers,
                )
                if not (200 <= resp.status_code < 300):
                    snippet = resp.text[:200]
                    raise RuntimeError(
                        f"Langfuse traces request failed: "
                        f"HTTP {resp.status_code}: {snippet}"
                    )
                body = resp.json()
                data = body.get("data") or []
                if not data:
                    break
                all_traces.extend(data)

                meta = body.get("meta")
                if isinstance(meta, dict):
                    total_pages = meta.get("totalPages")
                    if isinstance(total_pages, int) and page >= total_pages:
                        break
                page += 1

        records = [self._to_record(trace) for trace in all_traces]
        total_cost = sum(float(t.get("totalCost") or 0) for t in all_traces)
        return LoggedCost(
            total_cost=total_cost,
            record_count=len(records),
            records=records,
        )

    def prune_before(self, cutoff: datetime) -> int:
        """Delete logged traces older than *cutoff* (``timestamp < cutoff``).

        Time-based retention — keeps the cost log bounded while guaranteeing
        any window at/after *cutoff* stays fully reconcilable (the consumer
        reconciles only windows inside this horizon). Lists the oldest traces
        up to *cutoff* (``toTimestamp`` + ``timestamp.asc``) and bulk-deletes
        them in pages until none remain. Returns the count deleted; raises
        ``RuntimeError`` on any non-2xx response.
        """
        url = f"{self._base_url}/api/public/traces"
        headers = {"Authorization": self._auth_header()}
        deleted = 0
        with httpx.Client(timeout=_TIMEOUT) as client:
            while True:
                # page=1 + asc: after each delete the oldest shifts forward, so
                # page 1 keeps yielding the next oldest batch ≤ cutoff.
                resp = client.get(
                    url,
                    params={
                        "toTimestamp": cutoff.isoformat(),
                        "limit": _PAGE_LIMIT,
                        "page": 1,
                        "orderBy": "timestamp.asc",
                    },
                    headers=headers,
                )
                if not (200 <= resp.status_code < 300):
                    raise RuntimeError(
                        f"Langfuse traces list (prune) failed: "
                        f"HTTP {resp.status_code}: {resp.text[:200]}"
                    )
                data = resp.json().get("data") or []
                ids = [str(t["id"]) for t in data if t.get("id")]
                if not ids:
                    break
                del_resp = client.request(
                    "DELETE",
                    url,
                    json={"traceIds": ids},
                    headers=headers,
                )
                if not (200 <= del_resp.status_code < 300):
                    raise RuntimeError(
                        f"Langfuse traces delete (prune) failed: "
                        f"HTTP {del_resp.status_code}: {del_resp.text[:200]}"
                    )
                deleted += len(ids)
        return deleted

    @staticmethod
    def _to_record(trace: dict[str, Any]) -> CostRecord:
        """Build a :class:`CostRecord` from one Langfuse trace dict."""
        raw_ts = trace.get("timestamp") or trace.get("createdAt")
        timestamp = _parse_timestamp(raw_ts)
        return CostRecord(
            id=str(trace.get("id", "")),
            cost=float(trace.get("totalCost") or 0),
            timestamp=timestamp,
            session_id=trace.get("sessionId"),
            name=trace.get("name"),
        )


def _parse_timestamp(value: Any) -> datetime:
    """Parse an ISO-8601 timestamp (tolerating a trailing ``Z``)."""
    if isinstance(value, datetime):
        return value
    text = str(value or "")
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    return datetime.fromisoformat(text)
