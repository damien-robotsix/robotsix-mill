"""Periodic Langfuse trace cleanup.

Deletes the oldest traces from a repo's Langfuse project until the
project is at most ``max_traces`` rows.  Pure HTTP, no LLM — wired into
the worker via ``_langfuse_cleanup_poll_loop``.

Why count-capped (not time-capped): the self-hosted Langfuse instance
degrades on large trace tables (a simple ``totalItems`` count for ~3k
rows already takes >30s).  Keeping the table small keeps the UI and
``/api/public/traces`` queries snappy.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass

from ..config import RepoConfig, Settings, get_secrets

log = logging.getLogger(__name__)

_PAGE_SIZE = 100  # Langfuse API page-size cap


@dataclass
class CleanupResult:
    project: str
    traces_before: int
    traces_deleted: int


def run_langfuse_cleanup_pass(
    *,
    settings: Settings,
    repo_config: RepoConfig | None,
    max_traces: int,
) -> CleanupResult:
    """Run one cleanup sweep against *repo_config*'s Langfuse project.

    Returns the deletion count for logging.  Never raises — Langfuse
    outages are logged and the worker re-tries on the next interval.
    """
    import httpx

    if repo_config is not None:
        host = (repo_config.langfuse_base_url or "https://cloud.langfuse.com").rstrip(
            "/"
        )
        public_key = repo_config.langfuse_public_key
        secret_key = repo_config.langfuse_secret_key
        label = repo_config.repo_id
    else:
        host = (get_secrets().langfuse_base_url or "https://cloud.langfuse.com").rstrip(
            "/"
        )
        public_key = get_secrets().langfuse_public_key  # type: ignore[assignment]
        secret_key = get_secrets().langfuse_secret_key  # type: ignore[assignment]
        label = "default"

    if not (public_key and secret_key):
        log.info("langfuse_cleanup: %s — no credentials, skipping", label)
        return CleanupResult(project=label, traces_before=0, traces_deleted=0)
    if max_traces <= 0:
        log.info(
            "langfuse_cleanup: %s — max_traces=%d ≤ 0, skipping", label, max_traces
        )
        return CleanupResult(project=label, traces_before=0, traces_deleted=0)

    auth = "Basic " + base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    headers = {"Authorization": auth, "Content-Type": "application/json"}

    deleted_total = 0
    try:
        with httpx.Client(timeout=120, headers=headers) as c:
            # 1. Count.
            r = c.get(f"{host}/api/public/traces", params={"limit": 1, "page": 1})
            r.raise_for_status()
            total = int(r.json().get("meta", {}).get("totalItems", 0))
            if total <= max_traces:
                log.info(
                    "langfuse_cleanup: %s — %d traces (≤ cap %d), nothing to delete",
                    label,
                    total,
                    max_traces,
                )
                return CleanupResult(
                    project=label, traces_before=total, traces_deleted=0
                )

            to_delete = total - max_traces
            log.info(
                "langfuse_cleanup: %s — %d over cap, deleting %d oldest",
                label,
                to_delete,
                to_delete,
            )

            # 2. Delete oldest in batches of _PAGE_SIZE. Always fetch page=1
            # with timestamp.asc — after each delete the "oldest" shifts
            # forward but page=1 keeps returning the next oldest batch.
            while deleted_total < to_delete:
                batch_size = min(_PAGE_SIZE, to_delete - deleted_total)
                r = c.get(
                    f"{host}/api/public/traces",
                    params={
                        "limit": batch_size,
                        "page": 1,
                        "orderBy": "timestamp.asc",
                    },
                )
                r.raise_for_status()
                traces = r.json().get("data", [])
                ids = [t["id"] for t in traces if t.get("id")]
                if not ids:
                    log.warning(
                        "langfuse_cleanup: %s — list returned no IDs at "
                        "%d/%d, stopping",
                        label,
                        deleted_total,
                        to_delete,
                    )
                    break
                r = c.request(
                    "DELETE",
                    f"{host}/api/public/traces",
                    json={"traceIds": ids},
                )
                r.raise_for_status()
                deleted_total += len(ids)
                log.info(
                    "langfuse_cleanup: %s — deleted %d (%d/%d)",
                    label,
                    len(ids),
                    deleted_total,
                    to_delete,
                )
            return CleanupResult(
                project=label,
                traces_before=total,
                traces_deleted=deleted_total,
            )
    except Exception as e:  # noqa: BLE001 — periodic sweep must not crash worker
        log.exception(
            "langfuse_cleanup: %s — pass failed after deleting %d: %s",
            label,
            deleted_total,
            e,
        )
        return CleanupResult(
            project=label, traces_before=0, traces_deleted=deleted_total
        )
