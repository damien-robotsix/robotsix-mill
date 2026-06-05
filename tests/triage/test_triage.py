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

from robotsix_auto_mail.db import MailRecord, init_db, insert_record
from robotsix_auto_mail.triage import (
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
    _load_memory,
    _rule_fingerprint,
    apply_triage_rules,
    get_triage_decision,
    list_active_rules,
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
        "robotsix_auto_mail.triage.OpenRouterDeepseekProvider",
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
            "robotsix_auto_mail.triage.OpenRouterDeepseekProvider"
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
            "robotsix_auto_mail.triage.OpenRouterDeepseekProvider"
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
            "robotsix_auto_mail.triage.OpenRouterDeepseekProvider",
            return_value=mock_provider,
        ):
            with pytest.raises(TriageError) as exc:
                run_triage_agent(conn)
        assert "timeout" in str(exc.value)
        mock_handle.close.assert_called_once()
    finally:
        conn.close()


def test_run_triage_agent_does_not_touch_status_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Triage must not change the kanban status column."""
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    conn = init_db(":memory:")
    try:
        _insert_inbox(conn, "<a@x.com>")
        _handle, patcher = _patch_llm(
            TriageResult(items=[TriageItem(index=1, action="archive")])
        )
        with patcher:
            run_triage_agent(conn)
        # mail_records.status is still 'inbox'.
        row = conn.execute(
            "SELECT status FROM mail_records WHERE message_id = ?",
            ("<a@x.com>",),
        ).fetchone()
        assert row[0] == "inbox"
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
# Deterministic triage rules — models
# ---------------------------------------------------------------------------


def _seed_history(
    conn: object, sender: str, action: str, count: int, *, prefix: str = "m"
) -> None:
    """Insert *count* inbox records from *sender* triaged as *action*."""
    for i in range(count):
        mid = f"<{prefix}{i}@x.com>"
        _insert_inbox(conn, mid, sender=sender)
        set_triage_decision(  # type: ignore[arg-type]
            conn, mid, action, source="user"
        )


def test_triage_rule_rejects_invalid_match_type() -> None:
    with pytest.raises(pydantic.ValidationError):
        TriageRule(match_type="regex", match_value="x", action="archive")


def test_triage_rule_rejects_invalid_action() -> None:
    with pytest.raises(pydantic.ValidationError):
        TriageRule(match_type="sender", match_value="x", action="banana")


def test_rule_ledger_entry_rejects_unknown_state() -> None:
    rule = TriageRule(
        match_type="sender", match_value="a@x.com", action="archive"
    )
    with pytest.raises(pydantic.ValidationError):
        RuleLedgerEntry(rule=rule, state="bogus")


def _proposal(
    match_type: str = "sender",
    match_value: str = "a@x.com",
    action: str = "archive",
    *,
    title: str = "t",
    body: str = "b",
) -> TriageRuleProposal:
    return TriageRuleProposal(
        rule=TriageRule(
            match_type=match_type, match_value=match_value, action=action
        ),
        title=title,
        body=body,
    )


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------


def test_rule_fingerprint_stable_across_presentation() -> None:
    """Fingerprint ignores title/body/confidence wording."""
    a = _proposal(title="One", body="x")
    b = _proposal(title="Totally different", body="y")
    assert _rule_fingerprint(a) == _rule_fingerprint(b)


def test_rule_fingerprint_ignores_case_and_whitespace() -> None:
    a = _proposal(match_value="A@X.com", action="archive")
    b = _proposal(match_value="  a@x.com  ", action="archive")
    assert _rule_fingerprint(a) == _rule_fingerprint(b)


def test_rule_fingerprint_distinct_for_different_rules() -> None:
    base = _proposal(match_type="sender", match_value="a@x.com",
                     action="archive")
    other_type = _proposal(match_type="domain", match_value="a@x.com",
                           action="archive")
    other_value = _proposal(match_value="b@x.com")
    other_action = _proposal(action="delete")
    assert _rule_fingerprint(base) != _rule_fingerprint(other_type)
    assert _rule_fingerprint(base) != _rule_fingerprint(other_value)
    assert _rule_fingerprint(base) != _rule_fingerprint(other_action)


# ---------------------------------------------------------------------------
# propose_triage_rules
# ---------------------------------------------------------------------------


def test_propose_rules_above_threshold() -> None:
    """A consistent sender at/above threshold yields a sender rule."""
    conn = init_db(":memory:")
    try:
        _seed_history(conn, "news@a.com", "archive", 3)
        proposals = propose_triage_rules(conn)
        senders = [
            p.rule for p in proposals if p.rule.match_type == "sender"
        ]
        assert any(
            r.match_value == "news@a.com" and r.action == "archive"
            for r in senders
        )
    finally:
        conn.close()


def test_propose_rules_respects_threshold() -> None:
    """Below threshold (2 decisions) yields no rule."""
    conn = init_db(":memory:")
    try:
        _seed_history(conn, "news@a.com", "archive", 2)
        assert propose_triage_rules(conn) == []
    finally:
        conn.close()


def test_propose_rules_excludes_user_triage() -> None:
    """user_triage decisions never produce a rule."""
    conn = init_db(":memory:")
    try:
        _seed_history(conn, "news@a.com", "user_triage", 5)
        assert propose_triage_rules(conn) == []
    finally:
        conn.close()


def test_propose_rules_requires_consistency() -> None:
    """An inconsistent sender (mixed actions) yields no rule."""
    conn = init_db(":memory:")
    try:
        _insert_inbox(conn, "<a@x.com>", sender="news@a.com")
        _insert_inbox(conn, "<b@x.com>", sender="news@a.com")
        _insert_inbox(conn, "<c@x.com>", sender="news@a.com")
        set_triage_decision(conn, "<a@x.com>", "archive", source="user")
        set_triage_decision(conn, "<b@x.com>", "archive", source="user")
        set_triage_decision(conn, "<c@x.com>", "delete", source="user")
        senders = [
            p for p in propose_triage_rules(conn)
            if p.rule.match_type == "sender"
        ]
        assert senders == []
    finally:
        conn.close()


def test_propose_rules_domain_when_multiple_senders() -> None:
    """A domain spanning >=2 consistent senders yields a domain rule."""
    conn = init_db(":memory:")
    try:
        _seed_history(conn, "a@news.com", "archive", 2, prefix="a")
        _seed_history(conn, "b@news.com", "archive", 2, prefix="b")
        domains = [
            p.rule for p in propose_triage_rules(conn)
            if p.rule.match_type == "domain"
        ]
        assert any(
            r.match_value == "news.com" and r.action == "archive"
            for r in domains
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Dedup ledger & state transitions
# ---------------------------------------------------------------------------


def test_record_and_filter_rule_proposals_dedups() -> None:
    conn = init_db(":memory:")
    try:
        proposals = [_proposal(match_value="a@x.com"),
                     _proposal(match_value="b@x.com")]
        first = record_and_filter_rule_proposals(conn, proposals)
        assert len(first) == 2
        second = record_and_filter_rule_proposals(conn, proposals)
        assert second == []
    finally:
        conn.close()


def test_record_and_filter_suppresses_any_state() -> None:
    """A rejected proposal is not re-proposed."""
    conn = init_db(":memory:")
    try:
        proposal = _proposal()
        record_and_filter_rule_proposals(conn, [proposal])
        set_rule_state(conn, _rule_fingerprint(proposal), "rejected")
        assert record_and_filter_rule_proposals(conn, [proposal]) == []
    finally:
        conn.close()


def test_accept_adds_active_rule() -> None:
    conn = init_db(":memory:")
    try:
        proposal = _proposal(match_value="a@x.com", action="archive")
        record_and_filter_rule_proposals(conn, [proposal])
        set_rule_state(conn, _rule_fingerprint(proposal), "accepted")
        active = list_active_rules(conn)
        assert len(active) == 1
        assert active[0].match_value == "a@x.com"
        assert active[0].action == "archive"
    finally:
        conn.close()


def test_reject_does_not_add_active_rule() -> None:
    conn = init_db(":memory:")
    try:
        proposal = _proposal()
        record_and_filter_rule_proposals(conn, [proposal])
        set_rule_state(conn, _rule_fingerprint(proposal), "rejected")
        assert list_active_rules(conn) == []
    finally:
        conn.close()


def test_set_rule_state_invalid_state_raises() -> None:
    conn = init_db(":memory:")
    try:
        proposal = _proposal()
        record_and_filter_rule_proposals(conn, [proposal])
        with pytest.raises(TriageError):
            set_rule_state(conn, _rule_fingerprint(proposal), "bogus")
    finally:
        conn.close()


def test_set_rule_state_unknown_fingerprint_raises() -> None:
    conn = init_db(":memory:")
    try:
        with pytest.raises(TriageError):
            set_rule_state(conn, "deadbeefdeadbeef", "accepted")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# apply_triage_rules
# ---------------------------------------------------------------------------


def _activate(conn: object, proposal: TriageRuleProposal) -> None:
    record_and_filter_rule_proposals(conn, [proposal])  # type: ignore[arg-type]
    set_rule_state(conn, _rule_fingerprint(proposal), "accepted")  # type: ignore[arg-type]


def test_apply_rules_matches_sender() -> None:
    conn = init_db(":memory:")
    try:
        _activate(conn, _proposal(match_type="sender",
                                  match_value="news@a.com", action="archive"))
        record = MailRecord(
            message_id="<m@x.com>", sender="News@A.com",
            subject="Hi", date="2025-06-01T00:00:00",
        )
        assert apply_triage_rules(conn, record) == "archive"
    finally:
        conn.close()


def test_apply_rules_matches_domain() -> None:
    conn = init_db(":memory:")
    try:
        _activate(conn, _proposal(match_type="domain",
                                  match_value="news.com", action="delete"))
        record = MailRecord(
            message_id="<m@x.com>", sender="anyone@news.com",
            subject="Hi", date="2025-06-01T00:00:00",
        )
        assert apply_triage_rules(conn, record) == "delete"
    finally:
        conn.close()


def test_apply_rules_matches_subject_substring() -> None:
    conn = init_db(":memory:")
    try:
        _activate(conn, _proposal(match_type="subject_contains",
                                  match_value="invoice", action="answer"))
        record = MailRecord(
            message_id="<m@x.com>", sender="a@x.com",
            subject="Your INVOICE is ready", date="2025-06-01T00:00:00",
        )
        assert apply_triage_rules(conn, record) == "answer"
    finally:
        conn.close()


def test_apply_rules_returns_none_when_no_match() -> None:
    conn = init_db(":memory:")
    try:
        _activate(conn, _proposal(match_type="sender",
                                  match_value="news@a.com", action="archive"))
        record = MailRecord(
            message_id="<m@x.com>", sender="other@b.com",
            subject="Hi", date="2025-06-01T00:00:00",
        )
        assert apply_triage_rules(conn, record) is None
    finally:
        conn.close()


def test_apply_rules_none_without_active_rules() -> None:
    conn = init_db(":memory:")
    try:
        record = MailRecord(
            message_id="<m@x.com>", sender="a@x.com",
            subject="Hi", date="2025-06-01T00:00:00",
        )
        assert apply_triage_rules(conn, record) is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# run_triage_agent rule fast-path
# ---------------------------------------------------------------------------


def test_run_triage_agent_rule_matched_records_skip_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule-matched records are triaged deterministically; only the rest
    reach the LLM."""
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    conn = init_db(":memory:")
    try:
        _activate(conn, _proposal(match_type="sender",
                                  match_value="news@a.com", action="archive"))
        _insert_inbox(conn, "<ruled@x.com>", sender="news@a.com")
        _insert_inbox(conn, "<llm@x.com>", sender="alice@example.com")
        handle, patcher = _patch_llm(
            TriageResult(items=[TriageItem(index=1, action="answer")])
        )
        with patcher:
            out = run_triage_agent(conn)

        by_id = {d.message_id: d for d in out}
        assert by_id["<ruled@x.com>"].action == "archive"
        assert by_id["<ruled@x.com>"].reason == "matched deterministic rule"
        assert by_id["<llm@x.com>"].action == "answer"
        # Only the unmatched record was sent to the LLM.
        prompt = handle.run_sync.call_args.args[0]
        assert "alice@example.com" in prompt
        assert "news@a.com" not in prompt
    finally:
        conn.close()


def test_run_triage_agent_all_matched_skips_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When every inbox record matches a rule, the LLM is never built."""
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    conn = init_db(":memory:")
    try:
        _activate(conn, _proposal(match_type="sender",
                                  match_value="news@a.com", action="archive"))
        _insert_inbox(conn, "<ruled@x.com>", sender="news@a.com")
        with mock.patch(
            "robotsix_auto_mail.triage.OpenRouterDeepseekProvider"
        ) as cls:
            out = run_triage_agent(conn)
        assert [d.action for d in out] == ["archive"]
        cls.assert_not_called()
    finally:
        conn.close()
