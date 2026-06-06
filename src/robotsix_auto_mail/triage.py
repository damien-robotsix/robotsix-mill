"""LLM-driven inbox triage agent and local triage-decision persistence.

The triage agent classifies each ingested inbox ``MailRecord`` into an
*action status* — ``answer`` / ``archive`` / ``delete`` / ``ignore``, with
``user_triage`` as the explicit "the system does not know what to do"
fallback.  These action statuses are stored in the ``triage_decisions``
table AND, in addition, a triage decision now moves the mail's card on the
local kanban board by writing the ``status`` column owned by
:mod:`robotsix_auto_mail.status` via :func:`robotsix_auto_mail.status.set_status`.
Triage performs **NO IMAP / mailbox side effects** whatsoever: the kanban is
a local-only board, so moving a card never touches the original mailbox (no
archive / delete / move / expunge / append / store).

The ``pydantic_ai`` import is lazy to keep module-load time low, mirroring
:mod:`robotsix_auto_mail.config_sync`.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from email.utils import parseaddr

import pydantic
from robotsix_llmio.core import Tier

from robotsix_auto_mail.config import load_llm
from robotsix_auto_mail.db import (
    MailRecord,
    get_record_by_message_id,
    get_watermark,
    set_watermark,
)
from robotsix_auto_mail.format import _BODY_PREVIEW_LIMIT
from robotsix_auto_mail.status import list_by_status, set_status

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Canonical triage action vocabulary.  ``user_triage`` is the explicit
#: fallback meaning "the system does not know what to do".
VALID_TRIAGE_ACTIONS = frozenset(
    {"answer", "archive", "delete", "ignore", "user_triage"}
)

#: Maps each triage action to the kanban column its card moves to.
#: This is a LOCAL board move only — it never touches IMAP.
TRIAGE_ACTION_TO_STATUS: dict[str, str] = {
    "archive": "archive",
    "ignore": "done",
    "delete": "archive",  # user does NOT want real deletion — board column only
    "answer": "triaging",  # needs a human reply
    "user_triage": "triaging",  # system unsure → human decides
}

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

#: Accepted :class:`TriageRule` match types.
_VALID_RULE_MATCH_TYPES = frozenset({"sender", "domain", "subject_contains"})

#: Accepted :class:`RuleLedgerEntry` states.  All three suppress re-proposal
#: of a rule once it has been recorded (mirrors ``config_sync`` vocabulary).
_VALID_RULE_STATES = frozenset({"pending", "accepted", "rejected"})

#: Watermark key owned by this module for the rule-proposal dedup ledger.
#: Persisted in ``db.py``'s ``watermark`` key-value table as JSON (no new
#: tables / files), mirroring :mod:`robotsix_auto_mail.config_sync`.
_RULE_LEDGER_WATERMARK_KEY = "triage_rules_ledger"

#: Watermark key owned by this module for the accepted (active) rules list.
_RULE_ACTIVE_WATERMARK_KEY = "triage_rules_active"

#: Minimum number of consistent, non-``user_triage`` decisions before a rule
#: is proposed for a sender (or, in total, for a domain).
_RULE_MIN_DECISIONS = 3


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


class TriageRule(pydantic.BaseModel):
    """A deterministic triage rule mapping a match condition to an action.

    Matching is intentionally limited to three simple, non-regex kinds
    (``match_type``): ``sender`` (exact lowercased email address),
    ``domain`` (the sender's domain) and ``subject_contains`` (a
    case-insensitive subject substring).  ``action`` is validated against
    :data:`VALID_TRIAGE_ACTIONS`.
    """

    match_type: str
    match_value: str
    action: str

    @pydantic.field_validator("match_type")
    @classmethod
    def _validate_match_type(cls, v: str) -> str:
        if v not in _VALID_RULE_MATCH_TYPES:
            raise ValueError(
                "match_type must be one of "
                f"{sorted(_VALID_RULE_MATCH_TYPES)!r}; got {v!r}"
            )
        return v

    @pydantic.field_validator("action")
    @classmethod
    def _validate_action(cls, v: str) -> str:
        if v not in VALID_TRIAGE_ACTIONS:
            raise ValueError(
                "action must be one of "
                f"{sorted(VALID_TRIAGE_ACTIONS)!r}; got {v!r}"
            )
        return v


class TriageRuleProposal(TriageRule):
    """A :class:`TriageRule` plus human-readable presentation fields.

    Carries a ``title`` / ``body`` for display and a ``confidence`` level
    (``low`` / ``medium`` / ``high``) reflecting the strength of the
    evidence.  Its identity (fingerprint) is derived solely from the
    underlying rule's ``(match_type, match_value, action)``.
    """

    title: str = pydantic.Field(..., min_length=1)
    body: str = pydantic.Field(..., min_length=1)
    confidence: str = pydantic.Field(default="medium")

    @pydantic.field_validator("confidence")
    @classmethod
    def _validate_confidence(cls, v: str) -> str:
        if v not in _VALID_CONFIDENCE_LEVELS:
            raise ValueError(
                "confidence must be one of "
                f"{sorted(_VALID_CONFIDENCE_LEVELS)!r}; got {v!r}"
            )
        return v


class RuleLedgerEntry(pydantic.BaseModel):
    """One remembered rule proposal in the dedup memory ledger.

    Keyed (in the ledger dict) by the rule's stable fingerprint.  The
    ``match_type`` / ``match_value`` / ``action`` fields preserve the
    underlying :class:`TriageRule` so accepting a proposal can reconstruct
    it for the active-rules list.  The ``state`` (``pending`` / ``accepted``
    / ``rejected``) suppresses re-proposal in every state.
    """

    match_type: str
    match_value: str
    action: str
    title: str = ""
    state: str = "pending"

    @pydantic.field_validator("state")
    @classmethod
    def _validate_state(cls, v: str) -> str:
        if v not in _VALID_RULE_STATES:
            raise ValueError(
                "state must be one of "
                f"{sorted(_VALID_RULE_STATES)!r}; got {v!r}"
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

    In addition to the advisory upsert, this moves the mail's card on the
    local kanban board to the column mapped by
    :data:`TRIAGE_ACTION_TO_STATUS` via
    :func:`robotsix_auto_mail.status.set_status` — a local-only board move
    that performs NO IMAP / mailbox side effects.
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
    set_status(conn, message_id, TRIAGE_ACTION_TO_STATUS[action])


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


def _domain_key(sender: str) -> str:
    """Return the lowercased domain of *sender*, or ``""`` when absent."""
    key = _sender_key(sender)
    if "@" in key:
        return key.split("@", 1)[1]
    return ""


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
# Deterministic triage rules — proposal, dedup ledger and application
# ---------------------------------------------------------------------------


def _rule_fingerprint(rule: TriageRule) -> str:
    """Return a deterministic fingerprint identifying *rule*.

    Derived from the stable identity triple ``(match_type, match_value,
    action)`` — each stripped and lower-cased — and hashed with SHA-256.
    Presentation fields (``title`` / ``body`` / ``confidence``) are
    deliberately EXCLUDED so a reworded proposal keeps the same identity,
    mirroring :func:`config_sync._proposal_fingerprint`.
    """
    raw = (
        f"{rule.match_type.strip().lower()}"
        "\x00"
        f"{rule.match_value.strip().lower()}"
        "\x00"
        f"{rule.action.strip().lower()}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _confidence_for(count: int) -> str:
    """Map an evidence *count* to a confidence level."""
    if count >= 6:
        return "high"
    if count >= 4:
        return "medium"
    return "low"


def _load_rule_ledger(conn: sqlite3.Connection) -> dict[str, RuleLedgerEntry]:
    """Load the rule dedup ledger from the watermark table."""
    raw = get_watermark(conn, _RULE_LEDGER_WATERMARK_KEY)
    if raw is None:
        return {}
    data: dict[str, object] = json.loads(raw)
    return {
        fingerprint: RuleLedgerEntry.model_validate(entry)
        for fingerprint, entry in data.items()
    }


def _save_rule_ledger(
    conn: sqlite3.Connection, ledger: dict[str, RuleLedgerEntry]
) -> None:
    """Persist *ledger* to the watermark table (json round-trip)."""
    payload = {
        fingerprint: entry.model_dump()
        for fingerprint, entry in ledger.items()
    }
    set_watermark(conn, _RULE_LEDGER_WATERMARK_KEY, json.dumps(payload))


def _load_active_rules(conn: sqlite3.Connection) -> list[TriageRule]:
    """Load the accepted (active) deterministic rules from the watermark."""
    raw = get_watermark(conn, _RULE_ACTIVE_WATERMARK_KEY)
    if raw is None:
        return []
    data: list[object] = json.loads(raw)
    return [TriageRule.model_validate(entry) for entry in data]


def _save_active_rules(
    conn: sqlite3.Connection, rules: list[TriageRule]
) -> None:
    """Persist the active-rules list to the watermark table."""
    payload = [rule.model_dump() for rule in rules]
    set_watermark(conn, _RULE_ACTIVE_WATERMARK_KEY, json.dumps(payload))


def propose_triage_rules(
    conn: sqlite3.Connection,
) -> list[TriageRuleProposal]:
    """Derive candidate deterministic triage rules from triage history.

    Scans :func:`list_triage_decisions`, resolving each decision's sender
    via :func:`get_record_by_message_id`.  ``user_triage`` decisions and
    decisions whose mail is no longer present are ignored.  A *sender* rule
    is proposed when a single sender maps consistently to one non-
    ``user_triage`` action across at least :data:`_RULE_MIN_DECISIONS`
    decisions; a *domain* rule is proposed when at least two distinct senders
    in a domain all map to the same single action across at least
    :data:`_RULE_MIN_DECISIONS` decisions in total.  No LLM is involved.

    Proposals are returned sorted by ``(match_type, match_value, action)``
    for deterministic output; the caller is responsible for dedup via
    :func:`record_and_filter_rule_proposals`.
    """
    # sender_key -> list of non-user_triage actions
    by_sender: dict[str, list[str]] = {}
    # domain -> {sender_key -> list of non-user_triage actions}
    by_domain: dict[str, dict[str, list[str]]] = {}

    for decision in list_triage_decisions(conn):
        if decision.action == "user_triage":
            continue
        record = get_record_by_message_id(conn, decision.message_id)
        if record is None:
            continue
        sender = _sender_key(record.sender)
        by_sender.setdefault(sender, []).append(decision.action)
        domain = _domain_key(record.sender)
        if domain:
            by_domain.setdefault(domain, {}).setdefault(
                sender, []
            ).append(decision.action)

    proposals: list[TriageRuleProposal] = []

    # -- sender rules --
    for sender, actions in by_sender.items():
        distinct = set(actions)
        if len(distinct) != 1 or len(actions) < _RULE_MIN_DECISIONS:
            continue
        action = next(iter(distinct))
        count = len(actions)
        proposals.append(
            TriageRuleProposal(
                match_type="sender",
                match_value=sender,
                action=action,
                title=f"Triage mail from {sender} as {action}",
                body=(
                    f"All {count} triaged messages from `{sender}` were "
                    f"classified as `{action}`. Propose a deterministic rule "
                    f"so future mail from this sender is triaged as "
                    f"`{action}` without an LLM call."
                ),
                confidence=_confidence_for(count),
            )
        )

    # -- domain rules --
    for domain, senders in by_domain.items():
        if len(senders) < 2:
            continue
        all_actions = [a for actions in senders.values() for a in actions]
        distinct = set(all_actions)
        if len(distinct) != 1 or len(all_actions) < _RULE_MIN_DECISIONS:
            continue
        action = next(iter(distinct))
        count = len(all_actions)
        proposals.append(
            TriageRuleProposal(
                match_type="domain",
                match_value=domain,
                action=action,
                title=f"Triage mail from domain {domain} as {action}",
                body=(
                    f"{len(senders)} distinct senders in domain `{domain}` "
                    f"accounted for {count} triaged messages, all classified "
                    f"as `{action}`. Propose a deterministic rule so future "
                    f"mail from this domain is triaged as `{action}` without "
                    f"an LLM call."
                ),
                confidence=_confidence_for(count),
            )
        )

    proposals.sort(key=lambda p: (p.match_type, p.match_value, p.action))
    return proposals


def record_and_filter_rule_proposals(
    conn: sqlite3.Connection, proposals: list[TriageRuleProposal]
) -> list[TriageRuleProposal]:
    """Record genuinely-new rule proposals and filter already-seen ones.

    A proposal is *new* iff its fingerprint is absent from the ledger in
    ANY state — ``pending`` / ``accepted`` / ``rejected`` all suppress
    re-proposal.  New proposals are recorded as ``pending`` and returned in
    input order; the ledger is only written when there is something new,
    mirroring :func:`config_sync.record_and_filter_proposals`.
    """
    ledger = _load_rule_ledger(conn)
    new_proposals: list[TriageRuleProposal] = []
    for proposal in proposals:
        fingerprint = _rule_fingerprint(proposal)
        if fingerprint in ledger:
            continue
        ledger[fingerprint] = RuleLedgerEntry(
            match_type=proposal.match_type,
            match_value=proposal.match_value,
            action=proposal.action,
            title=proposal.title,
            state="pending",
        )
        new_proposals.append(proposal)
    if new_proposals:
        _save_rule_ledger(conn, ledger)
    return new_proposals


def set_rule_state(
    conn: sqlite3.Connection, fingerprint: str, state: str
) -> None:
    """Transition the ledger entry *fingerprint* to *state*.

    Accepting (``state == "accepted"``) adds the underlying
    :class:`TriageRule` to the active-rules list; any other state removes it
    (so rejecting never leaves an active rule behind).  Raises
    :class:`TriageError` for an invalid *state* or an unknown *fingerprint*.
    """
    if state not in _VALID_RULE_STATES:
        raise TriageError(
            "state must be one of "
            f"{sorted(_VALID_RULE_STATES)!r}; got {state!r}"
        )
    ledger = _load_rule_ledger(conn)
    entry = ledger.get(fingerprint)
    if entry is None:
        raise TriageError(
            f"No triage rule proposal with fingerprint {fingerprint!r}"
        )
    ledger[fingerprint] = entry.model_copy(update={"state": state})
    _save_rule_ledger(conn, ledger)

    rule = TriageRule(
        match_type=entry.match_type,
        match_value=entry.match_value,
        action=entry.action,
    )
    active = _load_active_rules(conn)
    present = any(_rule_fingerprint(r) == fingerprint for r in active)
    if state == "accepted":
        if not present:
            active.append(rule)
            _save_active_rules(conn, active)
    elif present:
        active = [
            r for r in active if _rule_fingerprint(r) != fingerprint
        ]
        _save_active_rules(conn, active)


def _rule_matches(rule: TriageRule, record: MailRecord) -> bool:
    """Return whether *record* matches *rule*."""
    value = rule.match_value.strip().lower()
    if rule.match_type == "sender":
        return _sender_key(record.sender) == value
    if rule.match_type == "domain":
        return _domain_key(record.sender) == value
    if rule.match_type == "subject_contains":
        return value in record.subject.lower()
    return False


def apply_triage_rules(
    conn: sqlite3.Connection, record: MailRecord
) -> str | None:
    """Return the action of the first active rule matching *record*.

    Matches by exact lowercased sender, sender domain, or case-insensitive
    subject substring (in active-rule order).  Returns ``None`` when no
    active rule matches.
    """
    for rule in _load_active_rules(conn):
        if _rule_matches(rule, record):
            return rule.action
    return None


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

    # -- deterministic rule fast-path: triage matching mail without the LLM --
    decisions: list[TriageDecision] = []
    remaining = []
    for record in records:
        matched_action = apply_triage_rules(conn, record)
        if matched_action is None:
            remaining.append(record)
            continue
        set_triage_decision(
            conn,
            record.message_id,
            matched_action,
            source="agent",
            reason="matched deterministic rule",
        )
        decisions.append(
            TriageDecision(
                message_id=record.message_id,
                action=matched_action,
                source="agent",
                reason="matched deterministic rule",
                confidence="medium",
            )
        )

    # Every inbox record was triaged deterministically — no LLM call needed.
    if not remaining:
        return decisions

    # -- resolve API key (arg -> LLM_API_KEY env -> config.llm_api_key) --
    resolved_key = api_key or os.environ.get("LLM_API_KEY", "")
    if not resolved_key:
        resolved_key, _ = load_llm()
    if not resolved_key:
        raise TriageError(
            "No LLM API key found — set the LLM_API_KEY environment "
            "variable or add an `llm.api_key` entry to your config file"
        )

    # -- lazy imports so the rest of the CLI works without pydantic_ai --
    from pydantic_ai import PromptedOutput
    from robotsix_llmio.openrouter_deepseek import OpenRouterDeepseekProvider

    # -- build agent --
    llm_provider = OpenRouterDeepseekProvider(api_key=resolved_key)
    agent_handle = llm_provider.build_agent(
        tier=tier,
        system_prompt=_build_triage_system_prompt(),
        output_type=PromptedOutput(TriageResult),
    )

    user_message = _build_user_message(remaining)

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
        if 1 <= item.index <= len(remaining) and item.index not in by_index:
            by_index[item.index] = item

    for i, record in enumerate(remaining, start=1):
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
