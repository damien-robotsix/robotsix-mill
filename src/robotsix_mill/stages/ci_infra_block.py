"""Detect the GitHub "account blocked / billing" CI-failure signature.

When GitHub disables **hosted** runners for an account — a failed payment
or a spending-limit cap — every hosted job is *refused before it starts*.
The workflow run concludes ``failure`` with an EMPTY ``steps`` array and
the only signal is a single check-run annotation:

    The job was not started because recent account payments have failed
    or your spending limit needs to be increased. Please check the
    'Billing & plans' section in your settings

This is neither a transient infrastructure flake (a re-run is refused the
same way) nor a code defect (no ci_fix edit can turn it green).  It is a
distinct *terminal* class — :data:`INFRA_ACCOUNT_BLOCKED` — that must:

* skip the ci_fix agent entirely (no LLM attempts, no cross-stage cycles);
* park the ticket ``BLOCKED`` with a note carrying
  :data:`INFRA_ACCOUNT_BLOCK_MARKER` so the recovery pass can find it;
* raise a *single* fleet-wide operator escalation (the condition is
  account-scoped, so one escalation covers every affected repo/ticket);
* auto-resume once a hosted job on the repo succeeds again.

Everything here is pure string / structure matching: no LLM, no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from typing import Any

# Terminal class name — sits alongside the existing transient/permanent
# (deterministic) CI-failure handling but is neither.
INFRA_ACCOUNT_BLOCKED = "INFRA_ACCOUNT_BLOCKED"

# Stable prefix of the ``BLOCKED`` note emitted for this class.  The
# account-block recovery pass keys on this literal to find parked tickets,
# so it must not be reworded without updating
# ``agents/runners/infra_account_block_runner`` (and vice-versa).
INFRA_ACCOUNT_BLOCK_MARKER = "Infra account blocked"

# Distinctive phrases from GitHub's billing / spending-limit annotation.
# Matched case-insensitively against every annotation message on the run.
_ACCOUNT_BLOCK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"recent account payments have failed", re.IGNORECASE),
    re.compile(r"spending limit needs to be increased", re.IGNORECASE),
    re.compile(
        r"job was not started because.*(?:payment|spending limit)",
        re.IGNORECASE | re.DOTALL,
    ),
)


def _messages_from_annotations(anns: Any) -> Iterator[str]:
    """Yield message strings from an ``annotations`` list.

    Each annotation may be a plain string or a dict with a ``message`` key.
    """
    if not isinstance(anns, Iterable) or isinstance(anns, str | bytes):
        return
    for ann in anns:
        if isinstance(ann, str):
            yield ann
        elif isinstance(ann, dict):
            msg = ann.get("message")
            if isinstance(msg, str):
                yield msg


def _messages_from_check(check: dict[str, Any]) -> Iterator[str]:
    """Yield every text a failing-check dict can carry the billing message in."""
    yield from _messages_from_annotations(check.get("annotations"))
    for field in ("summary", "text"):
        val = check.get(field)
        if isinstance(val, str):
            yield val


def _annotation_texts(run: dict[str, Any]) -> Iterator[str]:
    """Yield every annotation message reachable from a *run* payload.

    Accepts both the top-level ``annotations`` list (as returned by
    ``GET /repos/{owner}/{repo}/check-runs/{id}/annotations``) and the
    per-failing-check ``annotations`` nested under ``failing`` (the shape
    ``Forge.check_status`` / ``commit_ci_conclusion`` return).
    """
    yield from _messages_from_annotations(run.get("annotations"))
    for check in run.get("failing") or []:
        if isinstance(check, dict):
            yield from _messages_from_check(check)


def annotations_indicate_account_block(run: dict[str, Any]) -> bool:
    """True when any annotation on *run* matches the billing/spending text."""
    return any(
        pattern.search(text)
        for text in _annotation_texts(run)
        for pattern in _ACCOUNT_BLOCK_PATTERNS
    )


def _jobs(run: dict[str, Any]) -> list[dict[str, Any]]:
    return [j for j in (run.get("jobs") or []) if isinstance(j, dict)]


def _has_step_data(run: dict[str, Any]) -> bool:
    """True when at least one job carries a ``steps`` key (present or empty).

    When no job reports step data (e.g. the aggregate ``check_status``
    ``jobs`` list, which only has ``name``/``conclusion``), the empty-steps
    corroboration is simply unavailable and the annotation stands alone.
    """
    return any("steps" in j for j in _jobs(run))


def _has_stepless_job(run: dict[str, Any]) -> bool:
    """True when a job was refused before starting — an empty ``steps`` array."""
    return any(j.get("steps") == [] for j in _jobs(run))


def classify_ci_run(run: dict[str, Any]) -> str | None:
    """Classify *run* as :data:`INFRA_ACCOUNT_BLOCKED` or ``None``.

    Returns :data:`INFRA_ACCOUNT_BLOCKED` when the billing/spending-limit
    annotation is present.  When step data IS available it must corroborate
    the signature (at least one job refused before starting, i.e. an empty
    ``steps`` array) — this rejects a genuine code failure whose logs merely
    quote the billing text.  When no step data is available the annotation
    is authoritative on its own (it is account-scoped and unmistakable).

    *run* is a dict with any of: ``jobs`` (each optionally ``steps``),
    ``annotations`` (flat list), ``failing`` (check dicts with nested
    ``annotations``/``summary``/``text``).
    """
    if not annotations_indicate_account_block(run):
        return None
    if _has_step_data(run) and not _has_stepless_job(run):
        return None
    return INFRA_ACCOUNT_BLOCKED


def infra_account_block_note() -> str:
    """The ``BLOCKED`` note for a ticket parked on this class.

    Prefixed with :data:`INFRA_ACCOUNT_BLOCK_MARKER` so the account-block
    recovery pass can find and auto-resume the ticket.
    """
    return (
        f"{INFRA_ACCOUNT_BLOCK_MARKER}: GitHub refused to start hosted CI "
        "jobs for this repo (billing / spending-limit). This is an "
        "account-scoped infrastructure block, not a code defect — ci_fix "
        "was skipped. Auto-resumes once a hosted run on this repo succeeds."
    )
