"""Tests for the inbox triage agent and triage-decision persistence.

These exercise ``src/robotsix_auto_mail/triage.py``.
"""

from __future__ import annotations

import os
import tempfile
from unittest import mock

import pydantic
import pytest
from robotsix_llmio.core import Tier

from robotsix_auto_mail import status
from robotsix_auto_mail.db import MailRecord, init_db, insert_record
from robotsix_auto_mail.triage import (
    TRIAGE_ACTION_TO_STATUS,
    VALID_TRIAGE_ACTIONS,
    RuleLedgerEntry,
    SenderMemory,
    TriageDecision,
    TriageError,
    TriageItem,
    TriageResult,
    TriageRule,
    TriageRuleProposal,
    _build_memory_guidance,
    _load_active_rules,
    _load_memory,
    _load_rule_ledger,
    _rule_fingerprint,
    apply_triage_rules,
    get_triage_decision,
    list_triage_decisions,
    propose_triage_rules,
    record_and_filter_rule_proposals,
    record_human_decision,
    run_triage_agent,
    set_rule_state,
    set_triage_decision,
)


def _patch_llm(
    result_obj: TriageResult,
) -> tuple[mock.MagicMock, mock._patch[mock.MagicMock]]:
    """Patch OpenRouterDeepseekProvider to return *result_obj* from the LLM.

    Returns the mock handle (to assert ``close()``) and the patcher.
    """
    mock_run_result = mock.MagicMock()
    mock_run_result.output = result_obj
    mock_handle = mock.MagicMock()
    mock_handle.run_sync.return_value = mock_run_result

    mock_provider = mock.MagicMock()
    mock_provider.build_agent.return_value = mock_handle
    mock_provider.call_with_retry.side_effect = lambda fn, what: fn()

    patcher = mock.patch(
        "robotsix_llmio.openrouter_deepseek.OpenRouterDeepseekProvider",
        return_value=mock_provider,
    )
    return mock_handle, patcher


def _insert_inbox(conn: object, message_id: str, **overrides: str) -> None:
    """Insert an inbox MailRecord with sensible defaults."""
    record = MailRecord(
        message_id=message_id,
        sender=overrides.get("sender", "alice@example.com"),
        subject=overrides.get("subject", "Hello"),
        date="2025-06-01T12:00:00",
        status=overrides.get("status", "inbox"),
        body_plain=overrides.get("body_plain", "Just checking in!"),
    )
    insert_record(conn, record)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


def test_triage_item_defaults() -> None:
    """action defaults to user_triage, confidence to medium, reason to ''."""
    item = TriageItem(index=1)
    assert item.action == "user_triage"
    assert item.confidence == "medium"
    assert item.reason == ""


def test_triage_item_coerces_unknown_action() -> None:
    """An unknown action is coerced to user_triage, not rejected."""
    item = TriageItem(index=1, action="banana")
    assert item.action == "user_triage"


def test_triage_item_rejects_index_below_one() -> None:
    """index must be >= 1."""
    with pytest.raises(pydantic.ValidationError):
        TriageItem(index=0)


def test_triage_item_rejects_unknown_confidence() -> None:
    """An out-of-set confidence raises a pydantic ValidationError."""
    with pytest.raises(pydantic.ValidationError):
        TriageItem(index=1, confidence="bogus")


def test_triage_result_defaults_empty() -> None:
    """items defaults to an empty list."""
    assert TriageResult().items == []


def test_triage_decision_rejects_invalid_action() -> None:
    with pytest.raises(pydantic.ValidationError):
        TriageDecision(message_id="<a>", action="banana", source="user")


def test_triage_decision_rejects_invalid_source() -> None:
    with pytest.raises(pydantic.ValidationError):
        TriageDecision(message_id="<a>", action="answer", source="robot")


def test_triage_error_is_exception() -> None:
    err = TriageError("boom")
    assert isinstance(err, Exception)
    assert str(err) == "boom"


# ---------------------------------------------------------------------------
# set_triage_decision validation
# ---------------------------------------------------------------------------


def test_set_triage_decision_rejects_invalid_action() -> None:
    conn = init_db(":memory:")
    try:
        _insert_inbox(conn, "<a@x.com>")
        with pytest.raises(TriageError):
            set_triage_decision(conn, "<a@x.com>", "banana", source="user")
    finally:
        conn.close()


def test_set_triage_decision_rejects_invalid_source() -> None:
    conn = init_db(":memory:")
    try:
        _insert_inbox(conn, "<a@x.com>")
        with pytest.raises(TriageError):
            set_triage_decision(conn, "<a@x.com>", "answer", source="robot")
    finally:
        conn.close()


def test_set_triage_decision_upserts() -> None:
    """A second call for the same message_id overwrites the first."""
    conn = init_db(":memory:")
    try:
        _insert_inbox(conn, "<a@x.com>")
        set_triage_decision(conn, "<a@x.com>", "answer", source="agent")
        set_triage_decision(
            conn, "<a@x.com>", "archive", source="user", reason="mine"
        )
        decision = get_triage_decision(conn, "<a@x.com>")
        assert decision is not None
        assert decision.action == "archive"
        assert decision.source == "user"
        assert decision.reason == "mine"
        # Still exactly one row.
        assert len(list_triage_decisions(conn)) == 1
    finally:
        conn.close()


def test_get_triage_decision_missing_returns_none() -> None:
    conn = init_db(":memory:")
    try:
        assert get_triage_decision(conn, "<nope@x.com>") is None
    finally:
        conn.close()


def test_list_triage_decisions_filters_by_source() -> None:
    conn = init_db(":memory:")
    try:
        _insert_inbox(conn, "<a@x.com>")
        _insert_inbox(conn, "<b@x.com>")
        set_triage_decision(conn, "<a@x.com>", "answer", source="agent")
        set_triage_decision(conn, "<b@x.com>", "archive", source="user")
        agent_only = list_triage_decisions(conn, source="agent")
        assert [d.message_id for d in agent_only] == ["<a@x.com>"]
        user_only = list_triage_decisions(conn, source="user")
        assert [d.message_id for d in user_only] == ["<b@x.com>"]
        assert len(list_triage_decisions(conn)) == 2
    finally:
        conn.close()


def test_triage_decision_persists_across_connections() -> None:
    """A decision written on one connection is visible on another."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn1 = init_db(path)
        _insert_inbox(conn1, "<persisted@x.com>")
        set_triage_decision(
            conn1, "<persisted@x.com>", "answer", source="user"
        )
        conn1.close()

        conn2 = init_db(path)
        decision = get_triage_decision(conn2, "<persisted@x.com>")
        assert decision is not None
        assert decision.action == "answer"
        assert decision.source == "user"
        conn2.close()
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# run_triage_agent
# ---------------------------------------------------------------------------


def test_run_triage_agent_empty_inbox_no_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty inbox returns [] without invoking the LLM."""
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    conn = init_db(":memory:")
    try:
        with mock.patch(
            "robotsix_llmio.openrouter_deepseek.OpenRouterDeepseekProvider"
        ) as cls:
            out = run_triage_agent(conn)
        assert out == []
        cls.assert_not_called()
    finally:
        conn.close()


def test_run_triage_agent_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Indices map to message_ids; decisions persisted with source='agent'."""
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    conn = init_db(":memory:")
    try:
        _insert_inbox(conn, "<a@x.com>")
        _insert_inbox(conn, "<b@x.com>")
        result_obj = TriageResult(
            items=[
                TriageItem(index=1, action="answer", confidence="high"),
                TriageItem(index=2, action="archive", reason="keep"),
            ]
        )
        handle, patcher = _patch_llm(result_obj)
        with patcher:
            out = run_triage_agent(conn)

        assert [(d.message_id, d.action) for d in out] == [
            ("<a@x.com>", "answer"),
            ("<b@x.com>", "archive"),
        ]
        # Persisted with source='agent'.
        stored = list_triage_decisions(conn)
        assert all(d.source == "agent" for d in stored)
        assert get_triage_decision(conn, "<a@x.com>").action == "answer"  # type: ignore[union-attr]
        assert get_triage_decision(conn, "<b@x.com>").reason == "keep"  # type: ignore[union-attr]
        handle.close.assert_called_once()
    finally:
        conn.close()


def test_run_triage_agent_uses_cheap_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_agent is called with Tier.CHEAP by default."""
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    conn = init_db(":memory:")
    try:
        _insert_inbox(conn, "<a@x.com>")
        _handle, patcher = _patch_llm(
            TriageResult(items=[TriageItem(index=1, action="answer")])
        )
        with patcher as cls:
            run_triage_agent(conn)
            provider = cls.return_value
        provider.build_agent.assert_called_once()
        assert provider.build_agent.call_args.kwargs["tier"] == Tier.CHEAP
    finally:
        conn.close()


def test_run_triage_agent_clamps_unknown_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown action coerces to user_triage."""
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    conn = init_db(":memory:")
    try:
        _insert_inbox(conn, "<a@x.com>")
        # The model coerces unknown -> user_triage at validation time.
        result_obj = TriageResult(items=[TriageItem(index=1, action="weird")])
        _handle, patcher = _patch_llm(result_obj)
        with patcher:
            out = run_triage_agent(conn)
        assert out[0].action == "user_triage"
    finally:
        conn.close()


def test_run_triage_agent_omitted_record_defaults_user_triage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inbox record the LLM omitted defaults to user_triage."""
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    conn = init_db(":memory:")
    try:
        _insert_inbox(conn, "<a@x.com>")
        _insert_inbox(conn, "<b@x.com>")
        # Only index 1 returned; index 2 omitted.
        result_obj = TriageResult(
            items=[TriageItem(index=1, action="answer")]
        )
        _handle, patcher = _patch_llm(result_obj)
        with patcher:
            out = run_triage_agent(conn)
        by_id = {d.message_id: d.action for d in out}
        assert by_id == {
            "<a@x.com>": "answer",
            "<b@x.com>": "user_triage",
        }
    finally:
        conn.close()


def test_run_triage_agent_missing_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """No api_key, no env, no config key → TriageError; LLM not built."""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("MAIL_CONFIG_PATH", str(tmp_path / "missing.yaml"))  # type: ignore[operator]
    conn = init_db(":memory:")
    try:
        _insert_inbox(conn, "<a@x.com>")
        with mock.patch(
            "robotsix_llmio.openrouter_deepseek.OpenRouterDeepseekProvider"
        ) as cls:
            with pytest.raises(TriageError) as exc:
                run_triage_agent(conn, api_key=None)
        assert "LLM_API_KEY" in str(exc.value)
        cls.assert_not_called()
    finally:
        conn.close()


def test_run_triage_agent_llm_failure_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call_with_retry failure is wrapped as TriageError; close runs."""
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    conn = init_db(":memory:")
    try:
        _insert_inbox(conn, "<a@x.com>")
        mock_handle = mock.MagicMock()
        mock_provider = mock.MagicMock()
        mock_provider.build_agent.return_value = mock_handle
        mock_provider.call_with_retry.side_effect = RuntimeError("timeout")
        with mock.patch(
            "robotsix_llmio.openrouter_deepseek.OpenRouterDeepseekProvider",
            return_value=mock_provider,
        ):
            with pytest.raises(TriageError) as exc:
                run_triage_agent(conn)
        assert "timeout" in str(exc.value)
        mock_handle.close.assert_called_once()
    finally:
        conn.close()


def test_run_triage_agent_moves_status_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Triage moves the kanban status column to the mapped column."""
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    conn = init_db(":memory:")
    try:
        _insert_inbox(conn, "<a@x.com>")
        _handle, patcher = _patch_llm(
            TriageResult(items=[TriageItem(index=1, action="archive")])
        )
        with patcher:
            run_triage_agent(conn)
        # mail_records.status moved to 'archive' (archive -> archive column).
        row = conn.execute(
            "SELECT status FROM mail_records WHERE message_id = ?",
            ("<a@x.com>",),
        ).fetchone()
        assert row[0] == "archive"
    finally:
        conn.close()


def test_triage_action_to_status_mapping_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each action moves the card to its mapped, valid kanban column."""
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    # Keys agree with the action vocabulary; values are valid columns.
    assert set(TRIAGE_ACTION_TO_STATUS) == set(VALID_TRIAGE_ACTIONS)
    assert all(v in status.VALID_STATUSES for v in TRIAGE_ACTION_TO_STATUS.values())
    expected = {
        "archive": "archive",
        "ignore": "done",
        "delete": "archive",
        "answer": "triaging",
        "user_triage": "triaging",
    }
    for action, column in expected.items():
        conn = init_db(":memory:")
        try:
            _insert_inbox(conn, "<a@x.com>")
            _handle, patcher = _patch_llm(
                TriageResult(items=[TriageItem(index=1, action=action)])
            )
            with patcher:
                run_triage_agent(conn)
            row = conn.execute(
                "SELECT status FROM mail_records WHERE message_id = ?",
                ("<a@x.com>",),
            ).fetchone()
            assert row[0] == column
        finally:
            conn.close()


def test_run_triage_agent_performs_no_imap_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The triage path moves the card but performs ZERO IMAP calls."""
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    conn = init_db(":memory:")
    try:
        _insert_inbox(conn, "<a@x.com>")
        _handle, patcher = _patch_llm(
            TriageResult(items=[TriageItem(index=1, action="archive")])
        )
        with (
            patcher,
            mock.patch("imaplib.IMAP4") as imap4,
            mock.patch("imaplib.IMAP4_SSL") as imap4_ssl,
        ):
            run_triage_agent(conn)
        # (a) status moved to the mapped column.
        row = conn.execute(
            "SELECT status FROM mail_records WHERE message_id = ?",
            ("<a@x.com>",),
        ).fetchone()
        assert row[0] == "archive"
        # (b) no IMAP constructor was ever called.
        assert imap4.call_count == 0
        assert imap4_ssl.call_count == 0
    finally:
        conn.close()


def test_valid_triage_actions_vocabulary() -> None:
    assert VALID_TRIAGE_ACTIONS == frozenset(
        {"answer", "archive", "delete", "ignore", "user_triage"}
    )


# ---------------------------------------------------------------------------
# Human-decision memory ledger
# ---------------------------------------------------------------------------


def test_load_memory_empty_when_unset() -> None:
    """An unwritten memory loads as an empty dict."""
    conn = init_db(":memory:")
    try:
        assert _load_memory(conn) == {}
    finally:
        conn.close()


def test_record_human_decision_creates_entry() -> None:
    """A first decision creates a count-1 entry keyed by lowercased sender."""
    conn = init_db(":memory:")
    try:
        _insert_inbox(conn, "<a@x.com>", sender="Alice@Example.com")
        record_human_decision(conn, "<a@x.com>", "archive")
        memory = _load_memory(conn)
        assert "alice@example.com" in memory
        entry = memory["alice@example.com"]
        assert isinstance(entry, SenderMemory)
        assert entry.action == "archive"
        assert entry.count == 1
        assert entry.last_action == "archive"
        assert entry.updated_at != ""
    finally:
        conn.close()


def test_record_human_decision_increments_and_tracks_latest() -> None:
    """Repeated decisions increment count and reflect the latest action."""
    conn = init_db(":memory:")
    try:
        _insert_inbox(conn, "<a@x.com>", sender="alice@example.com")
        _insert_inbox(conn, "<b@x.com>", sender="alice@example.com")
        record_human_decision(conn, "<a@x.com>", "archive")
        record_human_decision(conn, "<b@x.com>", "delete")
        entry = _load_memory(conn)["alice@example.com"]
        assert entry.action == "delete"
        assert entry.count == 2
        assert entry.last_action == "archive"
    finally:
        conn.close()


def test_record_human_decision_rejects_invalid_action() -> None:
    conn = init_db(":memory:")
    try:
        _insert_inbox(conn, "<a@x.com>")
        with pytest.raises(TriageError):
            record_human_decision(conn, "<a@x.com>", "banana")
    finally:
        conn.close()


def test_record_human_decision_unknown_message_is_noop() -> None:
    """An unknown message_id records nothing."""
    conn = init_db(":memory:")
    try:
        record_human_decision(conn, "<missing@x.com>", "archive")
        assert _load_memory(conn) == {}
    finally:
        conn.close()


def test_memory_persists_across_connections() -> None:
    """Memory written on one connection is visible on another."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn1 = init_db(path)
        _insert_inbox(conn1, "<a@x.com>", sender="bob@example.com")
        record_human_decision(conn1, "<a@x.com>", "answer")
        conn1.close()

        conn2 = init_db(path)
        entry = _load_memory(conn2)["bob@example.com"]
        assert entry.action == "answer"
        assert entry.count == 1
        conn2.close()
    finally:
        os.unlink(path)


def test_agent_decisions_do_not_update_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_triage_agent (source='agent') leaves the human memory empty."""
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    conn = init_db(":memory:")
    try:
        _insert_inbox(conn, "<a@x.com>")
        _handle, patcher = _patch_llm(
            TriageResult(items=[TriageItem(index=1, action="archive")])
        )
        with patcher:
            run_triage_agent(conn)
        assert _load_memory(conn) == {}
    finally:
        conn.close()


def test_build_memory_guidance_empty() -> None:
    """Guidance is the empty string when the memory is empty."""
    conn = init_db(":memory:")
    try:
        assert _build_memory_guidance(conn) == ""
    finally:
        conn.close()


def test_build_memory_guidance_includes_sender_and_action() -> None:
    """Guidance names the sender and the remembered action."""
    conn = init_db(":memory:")
    try:
        _insert_inbox(conn, "<a@x.com>", sender="alice@example.com")
        record_human_decision(conn, "<a@x.com>", "archive")
        guidance = _build_memory_guidance(conn)
        assert "alice@example.com" in guidance
        assert "archive" in guidance
    finally:
        conn.close()


def test_run_triage_agent_prompt_includes_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When memory is non-empty, the LLM prompt carries the guidance."""
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    conn = init_db(":memory:")
    try:
        _insert_inbox(conn, "<a@x.com>", sender="alice@example.com")
        record_human_decision(conn, "<a@x.com>", "archive")
        handle, patcher = _patch_llm(
            TriageResult(items=[TriageItem(index=1, action="archive")])
        )
        with patcher:
            run_triage_agent(conn)
        prompt = handle.run_sync.call_args.args[0]
        assert "alice@example.com" in prompt
        assert "triaged by the user as `archive`" in prompt
    finally:
        conn.close()


def test_run_triage_agent_prompt_omits_guidance_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty memory keeps the guidance out of the LLM prompt."""
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    conn = init_db(":memory:")
    try:
        _insert_inbox(conn, "<a@x.com>")
        handle, patcher = _patch_llm(
            TriageResult(items=[TriageItem(index=1, action="archive")])
        )
        with patcher:
            run_triage_agent(conn)
        prompt = handle.run_sync.call_args.args[0]
        assert "triaged by the user" not in prompt
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Deterministic triage rules — proposal derivation
# ---------------------------------------------------------------------------


def _seed_decision(
    conn: object, message_id: str, sender: str, action: str
) -> None:
    """Insert an inbox record and a triage decision for it."""
    _insert_inbox(conn, message_id, sender=sender)
    set_triage_decision(conn, message_id, action, source="agent")  # type: ignore[arg-type]


def _accept_rule(
    conn: object, match_type: str, match_value: str, action: str
) -> str:
    """Record + accept a rule proposal; return its fingerprint."""
    proposal = TriageRuleProposal(
        match_type=match_type,
        match_value=match_value,
        action=action,
        title="t",
        body="b",
    )
    record_and_filter_rule_proposals(conn, [proposal])  # type: ignore[arg-type]
    fingerprint = _rule_fingerprint(proposal)
    set_rule_state(conn, fingerprint, "accepted")  # type: ignore[arg-type]
    return fingerprint


def test_propose_rules_sender_above_threshold() -> None:
    """A consistent sender at/above threshold yields one sender rule."""
    conn = init_db(":memory:")
    try:
        for i in range(3):
            _seed_decision(conn, f"<m{i}@x.com>", "alice@example.com", "archive")
        proposals = propose_triage_rules(conn)
        sender_rules = [p for p in proposals if p.match_type == "sender"]
        assert len(sender_rules) == 1
        assert sender_rules[0].match_value == "alice@example.com"
        assert sender_rules[0].action == "archive"
        assert sender_rules[0].confidence in {"low", "medium", "high"}
    finally:
        conn.close()


def test_propose_rules_respects_threshold() -> None:
    """Below the decision threshold no rule is proposed."""
    conn = init_db(":memory:")
    try:
        for i in range(2):
            _seed_decision(conn, f"<m{i}@x.com>", "alice@example.com", "archive")
        assert propose_triage_rules(conn) == []
    finally:
        conn.close()


def test_propose_rules_excludes_user_triage() -> None:
    """``user_triage`` decisions never drive a rule."""
    conn = init_db(":memory:")
    try:
        for i in range(4):
            _seed_decision(
                conn, f"<m{i}@x.com>", "alice@example.com", "user_triage"
            )
        assert propose_triage_rules(conn) == []
    finally:
        conn.close()


def test_propose_rules_inconsistent_no_rule() -> None:
    """A sender with conflicting actions yields no rule."""
    conn = init_db(":memory:")
    try:
        _seed_decision(conn, "<m0@x.com>", "alice@example.com", "archive")
        _seed_decision(conn, "<m1@x.com>", "alice@example.com", "archive")
        _seed_decision(conn, "<m2@x.com>", "alice@example.com", "delete")
        assert propose_triage_rules(conn) == []
    finally:
        conn.close()


def test_propose_rules_domain_when_multiple_senders() -> None:
    """Two senders in a domain, each below threshold, yield a domain rule."""
    conn = init_db(":memory:")
    try:
        for i in range(2):
            _seed_decision(conn, f"<a{i}@news.com>", "alice@news.com", "archive")
        for i in range(2):
            _seed_decision(conn, f"<b{i}@news.com>", "bob@news.com", "archive")
        proposals = propose_triage_rules(conn)
        domain_rules = [p for p in proposals if p.match_type == "domain"]
        assert len(domain_rules) == 1
        assert domain_rules[0].match_value == "news.com"
        assert domain_rules[0].action == "archive"
        # Neither sender hit the per-sender threshold individually.
        assert [p for p in proposals if p.match_type == "sender"] == []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fingerprint stability
# ---------------------------------------------------------------------------


def test_rule_fingerprint_ignores_case_and_whitespace() -> None:
    """Fingerprint is stable under case / surrounding whitespace."""
    a = TriageRule(
        match_type="sender", match_value="Alice@Example.com", action="archive"
    )
    b = TriageRule(
        match_type="sender",
        match_value="  alice@example.com  ",
        action="archive",
    )
    assert _rule_fingerprint(a) == _rule_fingerprint(b)


def test_rule_fingerprint_distinct_by_identity_fields() -> None:
    """Differing match_type / match_value / action give distinct fingerprints."""
    base = TriageRule(
        match_type="sender", match_value="alice@example.com", action="archive"
    )
    diff_action = TriageRule(
        match_type="sender", match_value="alice@example.com", action="delete"
    )
    diff_value = TriageRule(
        match_type="sender", match_value="bob@example.com", action="archive"
    )
    diff_type = TriageRule(
        match_type="domain", match_value="alice@example.com", action="archive"
    )
    fps = {
        _rule_fingerprint(r)
        for r in (base, diff_action, diff_value, diff_type)
    }
    assert len(fps) == 4


def test_rule_fingerprint_excludes_presentation() -> None:
    """Presentation fields (title/body/confidence) do not affect identity."""
    rule = TriageRule(
        match_type="sender", match_value="alice@example.com", action="archive"
    )
    proposal = TriageRuleProposal(
        match_type="sender",
        match_value="alice@example.com",
        action="archive",
        title="some title",
        body="some body",
        confidence="high",
    )
    assert _rule_fingerprint(rule) == _rule_fingerprint(proposal)


# ---------------------------------------------------------------------------
# Dedup ledger and state transitions
# ---------------------------------------------------------------------------


def test_record_and_filter_dedup_pending() -> None:
    """A re-proposed (already pending) finding is suppressed."""
    conn = init_db(":memory:")
    try:
        proposal = TriageRuleProposal(
            match_type="sender",
            match_value="alice@example.com",
            action="archive",
            title="t",
            body="b",
        )
        assert len(record_and_filter_rule_proposals(conn, [proposal])) == 1
        assert record_and_filter_rule_proposals(conn, [proposal]) == []
    finally:
        conn.close()


def test_record_and_filter_dedup_accepted_and_rejected() -> None:
    """Accepted and rejected findings are also suppressed on re-proposal."""
    conn = init_db(":memory:")
    try:
        accepted = TriageRuleProposal(
            match_type="sender",
            match_value="alice@example.com",
            action="archive",
            title="t",
            body="b",
        )
        rejected = TriageRuleProposal(
            match_type="sender",
            match_value="bob@example.com",
            action="delete",
            title="t",
            body="b",
        )
        record_and_filter_rule_proposals(conn, [accepted, rejected])
        set_rule_state(conn, _rule_fingerprint(accepted), "accepted")
        set_rule_state(conn, _rule_fingerprint(rejected), "rejected")
        assert (
            record_and_filter_rule_proposals(conn, [accepted, rejected]) == []
        )
    finally:
        conn.close()


def test_set_rule_state_accept_adds_active() -> None:
    """Accepting a proposal adds its rule to the active list and ledger."""
    conn = init_db(":memory:")
    try:
        proposal = TriageRuleProposal(
            match_type="domain",
            match_value="news.com",
            action="archive",
            title="t",
            body="b",
        )
        record_and_filter_rule_proposals(conn, [proposal])
        fingerprint = _rule_fingerprint(proposal)
        set_rule_state(conn, fingerprint, "accepted")

        active = _load_active_rules(conn)
        assert len(active) == 1
        assert active[0].match_type == "domain"
        assert active[0].match_value == "news.com"
        assert active[0].action == "archive"
        assert _load_rule_ledger(conn)[fingerprint].state == "accepted"
    finally:
        conn.close()


def test_set_rule_state_reject_not_added() -> None:
    """Rejecting a proposal does not add an active rule."""
    conn = init_db(":memory:")
    try:
        proposal = TriageRuleProposal(
            match_type="sender",
            match_value="alice@example.com",
            action="delete",
            title="t",
            body="b",
        )
        record_and_filter_rule_proposals(conn, [proposal])
        fingerprint = _rule_fingerprint(proposal)
        set_rule_state(conn, fingerprint, "rejected")
        assert _load_active_rules(conn) == []
        assert _load_rule_ledger(conn)[fingerprint].state == "rejected"
    finally:
        conn.close()


def test_set_rule_state_accept_then_reject_removes_active() -> None:
    """Rejecting a previously-accepted rule removes it from the active set."""
    conn = init_db(":memory:")
    try:
        fingerprint = _accept_rule(
            conn, "sender", "alice@example.com", "delete"
        )
        assert len(_load_active_rules(conn)) == 1
        set_rule_state(conn, fingerprint, "rejected")
        assert _load_active_rules(conn) == []
    finally:
        conn.close()


def test_set_rule_state_unknown_fingerprint_raises() -> None:
    """An unknown fingerprint raises TriageError naming the fingerprint."""
    conn = init_db(":memory:")
    try:
        with pytest.raises(TriageError) as exc:
            set_rule_state(conn, "deadbeef", "accepted")
        assert "deadbeef" in str(exc.value)
    finally:
        conn.close()


def test_set_rule_state_invalid_state_raises() -> None:
    """An invalid state raises TriageError."""
    conn = init_db(":memory:")
    try:
        proposal = TriageRuleProposal(
            match_type="sender",
            match_value="alice@example.com",
            action="delete",
            title="t",
            body="b",
        )
        record_and_filter_rule_proposals(conn, [proposal])
        with pytest.raises(TriageError):
            set_rule_state(conn, _rule_fingerprint(proposal), "bogus")
    finally:
        conn.close()


def test_rule_ledger_entry_rejects_invalid_state() -> None:
    """RuleLedgerEntry validates its state field."""
    with pytest.raises(pydantic.ValidationError):
        RuleLedgerEntry(
            match_type="sender",
            match_value="a@b.com",
            action="archive",
            state="bogus",
        )


# ---------------------------------------------------------------------------
# apply_triage_rules
# ---------------------------------------------------------------------------


def test_apply_triage_rules_matches_sender() -> None:
    """A sender rule matches by exact lowercased sender."""
    conn = init_db(":memory:")
    try:
        _accept_rule(conn, "sender", "bob@spam.com", "delete")
        record = MailRecord(
            message_id="<x>", sender="Bob@Spam.com", subject="hi", date="d"
        )
        assert apply_triage_rules(conn, record) == "delete"
    finally:
        conn.close()


def test_apply_triage_rules_matches_domain() -> None:
    """A domain rule matches by the sender's domain."""
    conn = init_db(":memory:")
    try:
        _accept_rule(conn, "domain", "spam.com", "delete")
        record = MailRecord(
            message_id="<x>",
            sender="Whoever <anyone@SPAM.com>",
            subject="hi",
            date="d",
        )
        assert apply_triage_rules(conn, record) == "delete"
    finally:
        conn.close()


def test_apply_triage_rules_matches_subject_substring() -> None:
    """A subject_contains rule matches a case-insensitive substring."""
    conn = init_db(":memory:")
    try:
        _accept_rule(conn, "subject_contains", "invoice", "archive")
        record = MailRecord(
            message_id="<x>",
            sender="a@b.com",
            subject="Your INVOICE is ready",
            date="d",
        )
        assert apply_triage_rules(conn, record) == "archive"
    finally:
        conn.close()


def test_apply_triage_rules_no_match_returns_none() -> None:
    """No active rule matching the record returns None."""
    conn = init_db(":memory:")
    try:
        _accept_rule(conn, "sender", "bob@spam.com", "delete")
        record = MailRecord(
            message_id="<x>", sender="carol@example.com", subject="hi", date="d"
        )
        assert apply_triage_rules(conn, record) is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# run_triage_agent — deterministic rule fast-path
# ---------------------------------------------------------------------------


def test_run_triage_agent_rule_match_skips_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All inbox mail matched by rules is triaged without an LLM call."""
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    conn = init_db(":memory:")
    try:
        _accept_rule(conn, "sender", "bob@spam.com", "delete")
        _insert_inbox(conn, "<bob@spam.com>", sender="bob@spam.com")
        with mock.patch(
            "robotsix_llmio.openrouter_deepseek.OpenRouterDeepseekProvider"
        ) as cls:
            out = run_triage_agent(conn)
        assert len(out) == 1
        assert out[0].action == "delete"
        assert out[0].reason == "matched deterministic rule"
        cls.assert_not_called()
        # Persisted with source='agent'.
        stored = get_triage_decision(conn, "<bob@spam.com>")
        assert stored is not None
        assert stored.source == "agent"
        assert stored.reason == "matched deterministic rule"
    finally:
        conn.close()


def test_run_triage_agent_only_unmatched_go_to_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule-matched mail is deterministic; only the rest reaches the LLM."""
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    conn = init_db(":memory:")
    try:
        _accept_rule(conn, "sender", "bob@spam.com", "delete")
        _insert_inbox(conn, "<bob@spam.com>", sender="bob@spam.com")
        _insert_inbox(conn, "<carol@x.com>", sender="carol@example.com")
        # The LLM only sees the single unmatched record at index 1.
        handle, patcher = _patch_llm(
            TriageResult(items=[TriageItem(index=1, action="answer")])
        )
        with patcher:
            out = run_triage_agent(conn)
        by_id = {d.message_id: d for d in out}
        assert by_id["<bob@spam.com>"].action == "delete"
        assert by_id["<bob@spam.com>"].reason == "matched deterministic rule"
        assert by_id["<carol@x.com>"].action == "answer"
        prompt = handle.run_sync.call_args.args[0]
        assert "carol@example.com" in prompt
        assert "bob@spam.com" not in prompt
    finally:
        conn.close()
