"""LLM-driven inbox triage agent and local triage-decision persistence.

The triage agent classifies each ingested inbox ``MailRecord`` into an
*action status* — ``answer`` / ``archive`` / ``delete`` / ``ignore``, with
``user_triage`` as the explicit "the system does not know what to do"
fallback.  These action statuses are **advisory local labels**: they are
stored only in the ``triage_decisions`` table and must NOT move the mail in
the original mailbox (no IMAP side effects) nor be written to the kanban
``status`` column owned by :mod:`robotsix_auto_mail.status`.

The ``pydantic_ai`` import is lazy to keep module-load time low, mirroring
:mod:`robotsix_auto_mail.config_sync`.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from email.utils import parseaddr

import pydantic
from robotsix_llmio.core import Tier
from robotsix_llmio.openrouter_deepseek import OpenRouterDeepseekProvider

from robotsix_auto_mail.config import load_llm
from robotsix_auto_mail.db import (
    get_record_by_message_id,
    get_watermark,
    set_watermark,
)
from robotsix_auto_mail.format import _BODY_PREVIEW_LIMIT
from robotsix_auto_mail.status import list_by_status

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Canonical triage action vocabulary.  ``user_triage`` is the explicit
#: fallback meaning "the system does not know what to do".
VALID_TRIAGE_ACTIONS = frozenset(
    {"answer", "archive", "delete", "ignore", "user_triage"}
)

#: Accepted decision sources.
_VALID_TRIAGE_SOURCES = frozenset({"agent", "user"})

#: Watermark key owned by this module for the persistent human-decision
#: memory.  The memory is persisted in ``db.py``'s ``watermark`` key-value
#: table — NOT a separate on-disk file — using the same ``json.dumps`` /
#: ``json.loads`` round-trip :mod:`robotsix_auto_mail.config_sync` uses for
#: its dedup ledger.  Reusing the watermark table keeps a single storage
#: mechanism and a single DB file instead of a parallel format.
_MEMORY_WATERMARK_KEY = "triage_human_memory"

#: Accepted confidence levels (mirrors ``DriftProposal.confidence``).
_VALID_CONFIDENCE_LEVELS = frozenset({"low", "medium", "high"})


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TriageError(Exception):
    """Raised when the inbox triage agent or persistence layer fails."""


# ---------------------------------------------------------------------------
# Pydantic models — structured LLM output contract
# ---------------------------------------------------------------------------


class TriageItem(pydantic.BaseModel):
    """One classified mail in the LLM response, referenced by 1-based index."""

    index: int = pydantic.Field(..., ge=1)
    #: Triage action.  Unknown / empty values are coerced to ``user_triage``
    #: rather than failing the whole batch.
    action: str = pydantic.Field(default="user_triage")
    reason: str = pydantic.Field(default="")
    #: Confidence level — one of ``low`` / ``medium`` / ``high``.
    confidence: str = pydantic.Field(default="medium")

    @pydantic.field_validator("action")
    @classmethod
    def _coerce_action(cls, v: str) -> str:
        if v not in VALID_TRIAGE_ACTIONS:
            return "user_triage"
        return v

    @pydantic.field_validator("confidence")
    @classmethod
    def _validate_confidence(cls, v: str) -> str:
        if v not in _VALID_CONFIDENCE_LEVELS:
            raise ValueError(
                "confidence must be one of "
                f"{sorted(_VALID_CONFIDENCE_LEVELS)!r}; got {v!r}"
            )
        return v


class TriageResult(pydantic.BaseModel):
    """Structured output the LLM must return — validated by pydantic.

    An empty ``items`` list is valid; any omitted inbox record is defaulted
    to ``user_triage`` by :func:`run_triage_agent`.
    """

    items: list[TriageItem] = pydantic.Field(default_factory=list)


class TriageDecision(pydantic.BaseModel):
    """A stored triage decision for a single mail, keyed by ``message_id``."""

    message_id: str
    action: str
    #: Who recorded the decision — ``agent`` or ``user``.
    source: str
    reason: str = ""
    confidence: str = "medium"

    @pydantic.field_validator("action")
    @classmethod
    def _validate_action(cls, v: str) -> str:
        if v not in VALID_TRIAGE_ACTIONS:
            raise ValueError(
                "action must be one of "
                f"{sorted(VALID_TRIAGE_ACTIONS)!r}; got {v!r}"
            )
        return v

    @pydantic.field_validator("source")
    @classmethod
    def _validate_source(cls, v: str) -> str:
        if v not in _VALID_TRIAGE_SOURCES:
            raise ValueError(
                "source must be one of "
                f"{sorted(_VALID_TRIAGE_SOURCES)!r}; got {v!r}"
            )
        return v


class SenderMemory(pydantic.BaseModel):
    """One sender's remembered human-triage preference.

    Stored in the human-decision memory ledger keyed by the lowercased
    sender email.  ``action`` is the most recent human action for the
    sender, ``last_action`` is the action recorded immediately before this
    one (equal to ``action`` for a brand-new entry), ``count`` is how many
    times the user has triaged mail from this sender and ``updated_at`` is
    the ISO-8601 UTC timestamp of the latest update.
    """

    action: str
    count: int = 1
    last_action: str = ""
    updated_at: str = ""

    @pydantic.field_validator("action", "last_action")
    @classmethod
    def _validate_action(cls, v: str) -> str:
        if v and v not in VALID_TRIAGE_ACTIONS:
            raise ValueError(
                "action must be one of "
                f"{sorted(VALID_TRIAGE_ACTIONS)!r}; got {v!r}"
            )
        return v


# ---------------------------------------------------------------------------
# Persistence helpers — triage_decisions table
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def set_triage_decision(
    conn: sqlite3.Connection,
    message_id: str,
    action: str,
    *,
    source: str,
    reason: str = "",
    confidence: str = "medium",
) -> None:
    """Upsert a triage decision for *message_id*.

    Validates *action* against :data:`VALID_TRIAGE_ACTIONS` and *source*
    against ``{"agent", "user"}`` (raising :class:`TriageError` otherwise),
    then upserts keyed on ``message_id`` and commits.  ``updated_at`` is set
    to an ISO-8601 UTC timestamp.
    """
    if action not in VALID_TRIAGE_ACTIONS:
        raise TriageError(
            "action must be one of "
            f"{sorted(VALID_TRIAGE_ACTIONS)!r}; got {action!r}"
        )
    if source not in _VALID_TRIAGE_SOURCES:
        raise TriageError(
            "source must be one of "
            f"{sorted(_VALID_TRIAGE_SOURCES)!r}; got {source!r}"
        )
    conn.execute(
        """\
INSERT INTO triage_decisions
    (message_id, action, source, reason, confidence, updated_at)
VALUES
    (:message_id, :action, :source, :reason, :confidence, :updated_at)
ON CONFLICT(message_id) DO UPDATE SET
    action = excluded.action,
    source = excluded.source,
    reason = excluded.reason,
    confidence = excluded.confidence,
    updated_at = excluded.updated_at
""",
        {
            "message_id": message_id,
            "action": action,
            "source": source,
            "reason": reason,
            "confidence": confidence,
            "updated_at": _utc_now_iso(),
        },
    )
    conn.commit()


def get_triage_decision(
    conn: sqlite3.Connection, message_id: str
) -> TriageDecision | None:
    """Return the stored :class:`TriageDecision` for *message_id*, or ``None``.

    Read-only — does **not** call ``conn.commit()``.
    """
    cur = conn.execute(
        "SELECT message_id, action, source, reason, confidence "
        "FROM triage_decisions WHERE message_id = ?",
        (message_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return TriageDecision(
        message_id=row[0],
        action=row[1],
        source=row[2],
        reason=row[3],
        confidence=row[4],
    )


def list_triage_decisions(
    conn: sqlite3.Connection, *, source: str | None = None
) -> list[TriageDecision]:
    """Return all stored triage decisions, ordered by ``message_id``.

    When *source* is given, only decisions with that source are returned.
    Read-only — does **not** call ``conn.commit()``.
    """
    if source is None:
        cur = conn.execute(
            "SELECT message_id, action, source, reason, confidence "
            "FROM triage_decisions ORDER BY message_id ASC"
        )
    else:
        cur = conn.execute(
            "SELECT message_id, action, source, reason, confidence "
            "FROM triage_decisions WHERE source = ? ORDER BY message_id ASC",
            (source,),
        )
    return [
        TriageDecision(
            message_id=row[0],
            action=row[1],
            source=row[2],
            reason=row[3],
            confidence=row[4],
        )
        for row in cur.fetchall()
    ]


# ---------------------------------------------------------------------------
# Human-decision memory ledger — watermark table
# ---------------------------------------------------------------------------


def _sender_key(sender: str) -> str:
    """Return the generalization key for *sender*.

    Extracts the bare email address (lowercased); falls back to the raw
    lowercased sender string when no address can be parsed.
    """
    address = parseaddr(sender)[1]
    return (address or sender).strip().lower()


def _load_memory(conn: sqlite3.Connection) -> dict[str, SenderMemory]:
    """Load the human-decision memory from the watermark table.

    Returns an empty dict when the memory has never been written.
    """
    raw = get_watermark(conn, _MEMORY_WATERMARK_KEY)
    if raw is None:
        return {}
    data: dict[str, object] = json.loads(raw)
    return {
        sender: SenderMemory.model_validate(entry)
        for sender, entry in data.items()
    }


def _save_memory(
    conn: sqlite3.Connection, memory: dict[str, SenderMemory]
) -> None:
    """Persist *memory* to the watermark table (json round-trip)."""
    payload = {
        sender: entry.model_dump() for sender, entry in memory.items()
    }
    set_watermark(conn, _MEMORY_WATERMARK_KEY, json.dumps(payload))


def record_human_decision(
    conn: sqlite3.Connection, message_id: str, action: str
) -> None:
    """Remember a human triage *action* for the sender of *message_id*.

    Looks up the sender via :func:`get_record_by_message_id`, updates that
    sender's :class:`SenderMemory` entry (incrementing ``count`` and moving
    ``action`` toward the latest human decision) and persists the memory.
    A no-op when *message_id* is unknown.  Validates *action* against
    :data:`VALID_TRIAGE_ACTIONS`.
    """
    if action not in VALID_TRIAGE_ACTIONS:
        raise TriageError(
            "action must be one of "
            f"{sorted(VALID_TRIAGE_ACTIONS)!r}; got {action!r}"
        )
    record = get_record_by_message_id(conn, message_id)
    if record is None:
        return
    key = _sender_key(record.sender)
    memory = _load_memory(conn)
    previous = memory.get(key)
    if previous is None:
        entry = SenderMemory(
            action=action,
            count=1,
            last_action=action,
            updated_at=_utc_now_iso(),
        )
    else:
        entry = SenderMemory(
            action=action,
            count=previous.count + 1,
            last_action=previous.action,
            updated_at=_utc_now_iso(),
        )
    memory[key] = entry
    _save_memory(conn, memory)


def _build_memory_guidance(conn: sqlite3.Connection) -> str:
    """Render the human-decision memory as concise prompt guidance.

    Returns one line per remembered sender (ordered by sender key) and an
    empty string when the memory is empty.
    """
    memory = _load_memory(conn)
    if not memory:
        return ""
    lines = [
        "Established human triage preferences (advisory — follow unless "
        "the new message clearly differs):"
    ]
    for sender in sorted(memory):
        entry = memory[sender]
        times = "time" if entry.count == 1 else "times"
        lines.append(
            f"- Mail from `{sender}` was triaged by the user as "
            f"`{entry.action}` ({entry.count} {times}) — prefer this "
            "unless the new message clearly differs."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


def _build_triage_system_prompt() -> str:
    """Build the LLM system prompt describing the triage task and actions."""
    return (
        "You are an inbox triage assistant. You are given a numbered list of "
        "incoming mail messages, each with a 1-based index, sender, subject, "
        "and a short body preview. Classify each message into exactly one "
        "action status:\n"
        "\n"
        "- `answer`: the message needs a personal reply.\n"
        "- `archive`: keep the message for reference but no reply is needed.\n"
        "- `delete`: the message is junk / worthless and can be discarded.\n"
        "- `ignore`: no action is needed and it need not be kept.\n"
        "- `user_triage`: you are NOT confident what to do — defer to a "
        "human. Use this whenever you are unsure.\n"
        "\n"
        "Reference each message by its 1-based `index` (do NOT echo back any "
        "message id). For each message return an `index`, an `action` (one "
        "of the values above), a short `reason`, and a `confidence` of "
        "`low`, `medium`, or `high`. Prefer `user_triage` over guessing.\n"
        "\n"
        "Return a JSON object with an `items` list. Return ONLY the JSON "
        "object matching the schema — no explanation, no markdown fences."
    )


def _body_preview(body: str) -> str:
    """Return a single-line body preview truncated to ``_BODY_PREVIEW_LIMIT``."""
    collapsed = " ".join(body.split())
    if len(collapsed) > _BODY_PREVIEW_LIMIT:
        return collapsed[:_BODY_PREVIEW_LIMIT] + "…"
    return collapsed


def _build_user_message(records: list) -> str:  # type: ignore[type-arg]
    """Enumerate *records* as ``index | sender | subject | <body preview>``."""
    lines = [
        "Messages to triage (index | sender | subject | body preview):"
    ]
    for i, record in enumerate(records, start=1):
        lines.append(
            f"{i} | {record.sender} | {record.subject} | "
            f"{_body_preview(record.body_plain)}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core agent
# ---------------------------------------------------------------------------


def run_triage_agent(
    conn: sqlite3.Connection,
    *,
    api_key: str | None = None,
    tier: Tier = Tier.CHEAP,
) -> list[TriageDecision]:
    """Classify every inbox mail into a triage action and persist the result.

    Reads inbox records via ``list_by_status(conn, "inbox")``; returns ``[]``
    immediately (without calling the LLM) when the inbox is empty.  Each
    returned ``TriageItem`` is mapped back to its ``MailRecord`` by 1-based
    index; unknown actions are clamped to ``user_triage`` and any omitted
    inbox record defaults to ``user_triage``.  Every decision is persisted
    with ``source='agent'``.

    Args:
        conn: Open SQLite connection.
        api_key: OpenRouter API key.  Resolves with the precedence
            ``api_key`` argument → ``LLM_API_KEY`` env var →
            ``config.llm_api_key`` (via :func:`load_llm`).
        tier: LLM tier to use.  ``Tier.CHEAP`` (default).

    Raises:
        TriageError: If the API key is missing or the LLM call fails.
    """
    records = list_by_status(conn, "inbox")
    if not records:
        return []

    # -- resolve API key (arg -> LLM_API_KEY env -> config.llm_api_key) --
    resolved_key = api_key or os.environ.get("LLM_API_KEY", "")
    if not resolved_key:
        resolved_key, _ = load_llm()
    if not resolved_key:
        raise TriageError(
            "No LLM API key found — set the LLM_API_KEY environment "
            "variable or add an `llm.api_key` entry to your config file"
        )

    # -- lazy import so the rest of the CLI works without pydantic_ai --
    from pydantic_ai import PromptedOutput

    # -- build agent --
    llm_provider = OpenRouterDeepseekProvider(api_key=resolved_key)
    agent_handle = llm_provider.build_agent(
        tier=tier,
        system_prompt=_build_triage_system_prompt(),
        output_type=PromptedOutput(TriageResult),
    )

    user_message = _build_user_message(records)

    # -- bias the model toward established human preferences (advisory) --
    guidance = _build_memory_guidance(conn)
    if guidance:
        user_message = f"{guidance}\n\n{user_message}"

    # -- call LLM --
    try:
        result = llm_provider.call_with_retry(
            lambda: agent_handle.run_sync(user_message),
            what="mail triage",
        )
    except Exception as exc:
        raise TriageError(str(exc)) from exc
    finally:
        agent_handle.close()

    output: TriageResult = result.output

    # -- map 1-based indices back to records; default omissions to user_triage --
    by_index: dict[int, TriageItem] = {}
    for item in output.items:
        if 1 <= item.index <= len(records) and item.index not in by_index:
            by_index[item.index] = item

    decisions: list[TriageDecision] = []
    for i, record in enumerate(records, start=1):
        matched = by_index.get(i)
        if matched is None:
            action, reason, confidence = "user_triage", "", "medium"
        else:
            action = matched.action
            if action not in VALID_TRIAGE_ACTIONS:
                action = "user_triage"
            reason, confidence = matched.reason, matched.confidence
        set_triage_decision(
            conn,
            record.message_id,
            action,
            source="agent",
            reason=reason,
            confidence=confidence,
        )
        decisions.append(
            TriageDecision(
                message_id=record.message_id,
                action=action,
                source="agent",
                reason=reason,
                confidence=confidence,
            )
        )
    return decisions
