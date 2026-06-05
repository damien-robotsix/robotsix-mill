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
from collections.abc import Iterator
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

    def _iter_pages(
        self,
        url: str,
        base_params: dict[str, Any],
        headers: dict[str, str],
        error_label: str,
    ) -> Iterator[list[dict[str, Any]]]:
        """Paginate ``url`` (1-based ``page``), yielding each page's ``data``.

        Owns the ``httpx.Client``, the page loop, the non-2xx ``RuntimeError``
        (labelled with *error_label*), the empty-``data`` break, and the
        ``meta.totalPages`` termination.
        """
        with httpx.Client(timeout=_TIMEOUT) as client:
            page = 1
            while True:
                resp = client.get(
                    url,
                    params={**base_params, "page": page},
                    headers=headers,
                )
                if not (200 <= resp.status_code < 300):
                    raise RuntimeError(
                        f"Langfuse {error_label} failed: "
                        f"HTTP {resp.status_code}: {resp.text[:200]}"
                    )
                body = resp.json()
                data = body.get("data") or []
                if not data:
                    break
                yield data
                meta = body.get("meta")
                if isinstance(meta, dict):
                    total_pages = meta.get("totalPages")
                    if isinstance(total_pages, int) and page >= total_pages:
                        break
                page += 1

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
        for data in self._iter_pages(url, base_params, headers, "traces request"):
            all_traces.extend(data)

        records = [self._to_record(trace) for trace in all_traces]
        total_cost = sum(float(t.get("totalCost") or 0) for t in all_traces)
        return LoggedCost(
            total_cost=total_cost,
            record_count=len(records),
            records=records,
        )

    def fetch_logged_cost_by_provider(
        self, window: CostWindow, provider: str
    ) -> LoggedCost:
        """Fetch logged GENERATION cost over *window*, summing only the slice
        whose observation metadata ``provider`` equals *provider*.

        The write path stamps ``langfuse.observation.metadata.provider`` (e.g.
        ``"openrouter"`` / ``"claude-sdk"``) so cost can be reconciled PER
        PROVIDER — an OpenRouter key only bills the OpenRouter slice, so a
        claude_sdk fleet (no independent billing API) reconciles 0-vs-0 instead
        of false-flagging all Claude spend. The public observations endpoint has
        no server-side metadata filter, so paginate ``type=GENERATION`` over the
        window and filter client-side. Raises ``RuntimeError`` on non-2xx.
        """
        url = f"{self._base_url}/api/public/observations"
        headers = {"Authorization": self._auth_header()}
        base_params: dict[str, Any] = {
            "type": "GENERATION",
            "fromStartTime": window.start.isoformat(),
            "toStartTime": window.end.isoformat(),
            "limit": _PAGE_LIMIT,
        }

        matched: list[dict[str, Any]] = []
        for data in self._iter_pages(url, base_params, headers, "observations request"):
            for obs in data:
                if _observation_provider(obs) == provider:
                    matched.append(obs)

        records = [self._observation_to_record(o) for o in matched]
        total_cost = sum(_observation_cost(o) for o in matched)
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

    @staticmethod
    def _observation_to_record(obs: dict[str, Any]) -> CostRecord:
        """Build a :class:`CostRecord` from one Langfuse observation dict.

        ``id`` is the observation id (not a trace id); ``session_id`` falls back
        to the parent ``traceId`` since observations carry no session directly.
        """
        raw_ts = obs.get("startTime") or obs.get("createdAt")
        timestamp = _parse_timestamp(raw_ts)
        return CostRecord(
            id=str(obs.get("id", "")),
            cost=_observation_cost(obs),
            timestamp=timestamp,
            session_id=obs.get("traceId"),
            name=obs.get("name"),
        )


def _observation_provider(obs: dict[str, Any]) -> str | None:
    """Pull the ``provider`` tag out of a Langfuse observation's metadata.

    The write path stamps ``langfuse.observation.metadata.provider``, which
    Langfuse surfaces under the observation's ``metadata`` dict.
    """
    metadata = obs.get("metadata")
    if isinstance(metadata, dict):
        provider = metadata.get("provider")
        if provider is not None:
            return str(provider)
    return None


def _observation_cost(obs: dict[str, Any]) -> float:
    """USD cost of one Langfuse observation.

    Prefers Langfuse's server-computed ``calculatedTotalCost``, then a raw
    ``totalCost``, then ``costDetails.total`` (mirrors the write-side rollup).
    """
    for key in ("calculatedTotalCost", "totalCost"):
        value = obs.get(key)
        if value is not None:
            return float(value or 0)
    cost_details = obs.get("costDetails")
    if isinstance(cost_details, dict) and cost_details.get("total") is not None:
        return float(cost_details["total"] or 0)
    return 0.0


def _parse_timestamp(value: Any) -> datetime:
    """Parse an ISO-8601 timestamp (tolerating a trailing ``Z``)."""
    if isinstance(value, datetime):
        return value
    text = str(value or "")
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    return datetime.fromisoformat(text)
