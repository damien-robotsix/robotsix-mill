"""Structured, machine-checkable block reasons.

A BLOCKED ticket's ``block_reason`` field carries a JSON object of the
form ``{"kind": <kind>, ...params}`` describing *why* it is blocked, so a
periodic rechecker can evaluate the condition against live state (GitHub
CI for ``target_branch_red``, ticket state for ``unmet_dependency``, llmio
status for ``quota_exhausted``) without substring-matching the prose note.

Kinds are stable strings so new ones can be added without a schema change;
the rechecker dispatches on ``kind`` and silently skips any kind it has no
evaluator for (it never resumes a ticket it cannot verify).
"""

from __future__ import annotations

import json

#: target branch's CI is red on workflows not introduced by this PR
TARGET_BRANCH_RED = "target_branch_red"
#: this ticket is blocked waiting on another ticket (``ticket_id``) to finish
UNMET_DEPENDENCY = "unmet_dependency"
#: a model provider's quota/credit is exhausted (``provider``)
QUOTA_EXHAUSTED = "quota_exhausted"


def encode(kind: str, **params: object) -> str:
    """Encode a structured block reason (kind + parameters) as JSON.

    The encoded form is stored in ``Ticket.block_reason`` and read by the
    periodic block-rechecker.  ``sort_keys`` keeps the encoding stable so
    it is diffable and idempotent.
    """
    return json.dumps({"kind": kind, **params}, sort_keys=True)


def decode(raw: str | None) -> dict | None:
    """Decode a ``block_reason`` string back to its dict.

    Returns ``None`` for ``None``, malformed JSON, or a value that is not
    an object with a ``kind`` key — i.e. anything that is not a usable
    structured reason.  Never raises.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except TypeError, ValueError:
        return None
    if not isinstance(data, dict) or not data.get("kind"):
        return None
    return data


def kind_of(raw: str | None) -> str | None:
    """Return just the block-reason kind, or ``None`` when absent/invalid."""
    data = decode(raw)
    return data["kind"] if data else None
