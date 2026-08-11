"""Hermetic tests for the notification subsystem (fleet + ntfy fallback)."""

from __future__ import annotations

import time

import httpx
import pytest

from robotsix_mill.core.states import State
from robotsix_mill.notify import send_notification
from robotsix_mill.notify.fleet import _DEDUP_WINDOW, _reset_fleet_notifier
from robotsix_mill.runtime.worker import process_ticket
from robotsix_mill.stages import Outcome, StageContext, registry
from robotsix_mill.stages.base import Stage


@pytest.fixture(autouse=True)
def _reset_notify():
    """Reset the fleet notifier singleton between tests."""
    _reset_fleet_notifier()


class _RecordingPost:
    """Drop-in replacement for ``httpx.post`` that records calls and
    returns a configurable response."""

    def __init__(self, status_code: int = 200, exc: Exception | None = None):
        self.calls: list[dict] = []
        self._status = status_code
        self._exc = exc

    def __call__(self, url, *, headers=None, content=None, timeout=None, json=None):
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "content": content,
                "json": json,
                "timeout": timeout,
            }
        )
        if self._exc is not None:
            raise self._exc
        return _FakeResponse(self._status)


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error",
                request=None,
                response=self,  # type: ignore[arg-type]
            )


@pytest.fixture
def ctx(settings, service, repo_config):
    return StageContext(settings=settings, service=service, repo_config=repo_config)


# ---------------------------------------------------------------------------
# send_notification unit tests
# ---------------------------------------------------------------------------


def test_noop_when_url_unset(settings, service, secrets_set):
    """When NTFY_URL is None / empty, send_notification returns immediately."""
    secrets_set(ntfy_url=None)
    t = service.create("x")
    send_notification(t, State.ERRORED, "boom", settings)


def test_noop_when_url_empty(settings, service, secrets_set):
    """Empty string is treated same as None."""
    secrets_set(ntfy_url="")
    t = service.create("x")
    send_notification(t, State.ERRORED, "boom", settings)


def test_posts_to_url(settings, service, monkeypatch, secrets_set):
    """Happy path: a POST is made with the correct headers and body."""
    secrets_set(ntfy_url="https://ntfy.sh/test")
    rec = _RecordingPost(200)
    monkeypatch.setattr(httpx, "post", rec)

    t = service.create("Add feature")
    send_notification(t, State.HUMAN_ISSUE_APPROVAL, "refined spec", settings)

    assert len(rec.calls) == 1
    c = rec.calls[0]
    assert c["url"] == "https://ntfy.sh/test"
    assert c["headers"]["X-Title"] == "mill: human_issue_approval - Add feature"
    assert c["headers"]["Content-Type"] == "text/plain"
    assert "Authorization" not in c["headers"]
    assert f"Ticket: {t.id}" in c["content"]
    assert "State: human_issue_approval" in c["content"]
    assert "Note: refined spec" in c["content"]
    assert "Board: http://127.0.0.1:8077" in c["content"]
    assert c["timeout"] is not None


def test_xtitle_header_is_ascii_safe(settings, service, monkeypatch, secrets_set):
    """Regression: an em-dash (or any non-ASCII ticket title) in the
    X-Title header made httpx raise UnicodeEncodeError, silently
    breaking EVERY notification. The header must be ASCII-encodable."""
    secrets_set(ntfy_url="https://ntfy.sh/test")
    rec = _RecordingPost(200)
    monkeypatch.setattr(httpx, "post", rec)

    t = service.create("Café — naïve ☕ build")  # non-ASCII title
    send_notification(
        t, State.HUMAN_MR_APPROVAL, "PR opened", settings
    )  # must not raise

    assert len(rec.calls) == 1
    xt = rec.calls[0]["headers"]["X-Title"]
    xt.encode("ascii")  # the actual failure mode — must not raise
    assert "—" not in xt  # em-dash gone
    assert xt.startswith("mill: human_mr_approval - ")


def test_note_none_renders_placeholder(settings, service, monkeypatch, secrets_set):
    """A None note becomes '(none)' in the body."""
    secrets_set(ntfy_url="https://ntfy.sh/test")
    rec = _RecordingPost(200)
    monkeypatch.setattr(httpx, "post", rec)

    t = service.create("x")
    send_notification(t, State.BLOCKED, None, settings)
    assert "Note: (none)" in rec.calls[0]["content"]


def test_token_sent_as_bearer(settings, service, monkeypatch, secrets_set):
    """NTFY_TOKEN is sent as an Authorization: Bearer header."""
    secrets_set(ntfy_url="https://ntfy.sh/test", ntfy_token="tk_secret")
    rec = _RecordingPost(200)
    monkeypatch.setattr(httpx, "post", rec)

    t = service.create("x")
    send_notification(t, State.HUMAN_MR_APPROVAL, "PR opened", settings)
    assert rec.calls[0]["headers"]["Authorization"] == "Bearer tk_secret"


def test_network_error_is_caught(settings, service, monkeypatch, caplog, secrets_set):
    """A POST exception does not propagate — it logs a warning and returns."""
    secrets_set(ntfy_url="https://ntfy.sh/test")
    rec = _RecordingPost(exc=ConnectionError("refused"))
    monkeypatch.setattr(httpx, "post", rec)

    t = service.create("x")
    send_notification(t, State.ERRORED, "fail", settings)
    assert rec.calls
    assert "ntfy notification failed" in caplog.text


def test_non_2xx_is_caught(settings, service, monkeypatch, caplog, secrets_set):
    """A 500 response is treated as a failure and logged, not raised."""
    secrets_set(ntfy_url="https://ntfy.sh/test")
    rec = _RecordingPost(status_code=500)
    monkeypatch.setattr(httpx, "post", rec)

    t = service.create("x")
    send_notification(t, State.BLOCKED, "stuck", settings)
    assert rec.calls
    assert "ntfy notification failed" in caplog.text


# ---------------------------------------------------------------------------
# Fleet notification tests
# ---------------------------------------------------------------------------


def test_fleet_noop_when_url_unset(settings, service, secrets_set):
    """When fleet_notify_url is None, no POST is made."""
    secrets_set(fleet_notify_url=None)
    t = service.create("x")
    send_notification(t, State.ERRORED, "boom", settings)


def test_fleet_posts_json_to_url(settings, service, monkeypatch, secrets_set):
    """Happy path: a JSON POST is made to the fleet endpoint."""
    secrets_set(fleet_notify_url="https://fleet.example.com/notify")
    rec = _RecordingPost(200)
    monkeypatch.setattr(httpx, "post", rec)

    t = service.create("Add feature")
    send_notification(t, State.HUMAN_ISSUE_APPROVAL, "refined spec", settings)

    assert len(rec.calls) == 1
    c = rec.calls[0]
    assert c["url"] == "https://fleet.example.com/notify"
    assert c["headers"]["Content-Type"] == "application/json"
    assert "Authorization" not in c["headers"]

    payload = c["json"]
    assert payload["source"] == "mill"
    assert payload["severity"] == "info"
    assert payload["category"] == "human_approval"
    assert payload["ticket_id"] == t.id
    assert payload["ticket_title"] == "Add feature"
    assert payload["state"] == "human_issue_approval"
    assert payload["note"] == "refined spec"
    assert payload["board_url"] == "http://127.0.0.1:8077"
    assert payload["suppressed_count"] == 0
    assert "timestamp" in payload


def test_fleet_severity_mapping(settings, service, monkeypatch, secrets_set):
    """Verify severity mapping: BLOCKED → critical, ERRORED → warning."""
    secrets_set(fleet_notify_url="https://fleet.example.com/notify")
    rec = _RecordingPost(200)
    monkeypatch.setattr(httpx, "post", rec)

    t = service.create("stuck")
    send_notification(t, State.BLOCKED, "timeout", settings)
    assert rec.calls[0]["json"]["severity"] == "critical"
    assert rec.calls[0]["json"]["category"] == "blocked"

    t2 = service.create("fail")
    send_notification(t2, State.ERRORED, "crash", settings)
    assert rec.calls[1]["json"]["severity"] == "warning"
    assert rec.calls[1]["json"]["category"] == "errored"


def test_fleet_token_sent_as_bearer(settings, service, monkeypatch, secrets_set):
    """fleet_notify_token is sent as Authorization: Bearer."""
    secrets_set(
        fleet_notify_url="https://fleet.example.com/notify",
        fleet_notify_token="tk_fleet",
    )
    rec = _RecordingPost(200)
    monkeypatch.setattr(httpx, "post", rec)

    t = service.create("x")
    send_notification(t, State.HUMAN_MR_APPROVAL, "PR opened", settings)
    assert rec.calls[0]["headers"]["Authorization"] == "Bearer tk_fleet"


def test_fleet_dedup_suppresses_within_window(
    settings, service, monkeypatch, secrets_set
):
    """Same (state, board) within dedup window → suppressed, not posted."""
    secrets_set(fleet_notify_url="https://fleet.example.com/notify")
    rec = _RecordingPost(200)
    monkeypatch.setattr(httpx, "post", rec)

    # Freeze time.
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)

    t1 = service.create("ticket 1")
    send_notification(t1, State.BLOCKED, "stuck", settings)
    assert len(rec.calls) == 1
    assert rec.calls[0]["json"]["suppressed_count"] == 0

    # Same state, same board, within window — suppressed.
    t2 = service.create("ticket 2")
    send_notification(t2, State.BLOCKED, "stuck too", settings)
    assert len(rec.calls) == 1  # no new POST

    t3 = service.create("ticket 3")
    send_notification(t3, State.BLOCKED, "another", settings)
    assert len(rec.calls) == 1  # still no new POST


def test_fleet_dedup_flushes_after_window(settings, service, monkeypatch, secrets_set):
    """After the dedup window, the next notification includes suppressed_count."""
    secrets_set(fleet_notify_url="https://fleet.example.com/notify")
    rec = _RecordingPost(200)
    monkeypatch.setattr(httpx, "post", rec)

    # Freeze time at 1000.
    fake_now = [1000.0]

    def _monotonic():
        return fake_now[0]

    monkeypatch.setattr(time, "monotonic", _monotonic)

    t1 = service.create("ticket 1")
    send_notification(t1, State.BLOCKED, "stuck", settings)
    assert len(rec.calls) == 1
    assert rec.calls[0]["json"]["suppressed_count"] == 0

    # Suppress two more within the window.
    t2 = service.create("ticket 2")
    send_notification(t2, State.BLOCKED, "stuck 2", settings)
    t3 = service.create("ticket 3")
    send_notification(t3, State.BLOCKED, "stuck 3", settings)
    assert len(rec.calls) == 1  # suppressed

    # Advance past the dedup window and send again.
    fake_now[0] += _DEDUP_WINDOW + 1.0
    t4 = service.create("ticket 4")
    send_notification(t4, State.BLOCKED, "still stuck", settings)

    assert len(rec.calls) == 2
    assert rec.calls[1]["json"]["suppressed_count"] == 2
    assert rec.calls[1]["json"]["ticket_id"] == t4.id


def test_fleet_different_boards_no_dedup(settings, service, monkeypatch, secrets_set):
    """Different board URLs produce different dedup keys — no suppression."""
    secrets_set(fleet_notify_url="https://fleet.example.com/notify")
    rec = _RecordingPost(200)
    monkeypatch.setattr(httpx, "post", rec)

    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)

    # First board
    t1 = service.create("ticket 1")
    send_notification(t1, State.BLOCKED, "stuck", settings)
    assert len(rec.calls) == 1

    # Change the api_url to simulate a different board.
    settings.api_url = "http://other-board:8077"
    t2 = service.create("ticket 2")
    send_notification(t2, State.BLOCKED, "stuck too", settings)

    # Different board → different dedup key → not suppressed.
    assert len(rec.calls) == 2


def test_fleet_network_error_is_caught(
    settings, service, monkeypatch, caplog, secrets_set
):
    """A POST exception does not propagate — logs warning, returns."""
    secrets_set(fleet_notify_url="https://fleet.example.com/notify")
    rec = _RecordingPost(exc=ConnectionError("refused"))
    monkeypatch.setattr(httpx, "post", rec)

    t = service.create("x")
    send_notification(t, State.ERRORED, "fail", settings)
    assert "fleet notify failed" in caplog.text


def test_fleet_non_2xx_is_caught(settings, service, monkeypatch, caplog, secrets_set):
    """A 500 response is logged, not raised."""
    secrets_set(fleet_notify_url="https://fleet.example.com/notify")
    rec = _RecordingPost(status_code=500)
    monkeypatch.setattr(httpx, "post", rec)

    t = service.create("x")
    send_notification(t, State.BLOCKED, "stuck", settings)
    assert "fleet notify failed" in caplog.text


def test_fallback_to_ntfy_when_fleet_unset(settings, service, monkeypatch, secrets_set):
    """When fleet is unset but ntfy is configured, ntfy fallback fires."""
    secrets_set(ntfy_url="https://ntfy.sh/test")
    rec = _RecordingPost(200)
    monkeypatch.setattr(httpx, "post", rec)

    t = service.create("title")
    send_notification(t, State.ERRORED, "boom", settings)

    assert len(rec.calls) == 1
    assert rec.calls[0]["url"] == "https://ntfy.sh/test"
    assert rec.calls[0]["headers"]["Content-Type"] == "text/plain"


def test_fleet_preferred_when_both_configured(
    settings, service, monkeypatch, secrets_set
):
    """When both fleet and ntfy are configured, fleet is used."""
    secrets_set(
        fleet_notify_url="https://fleet.example.com/notify",
        ntfy_url="https://ntfy.sh/test",
    )
    rec = _RecordingPost(200)
    monkeypatch.setattr(httpx, "post", rec)

    t = service.create("title")
    send_notification(t, State.ERRORED, "boom", settings)

    assert len(rec.calls) == 1
    assert rec.calls[0]["url"] == "https://fleet.example.com/notify"
    assert rec.calls[0]["headers"]["Content-Type"] == "application/json"


# ---------------------------------------------------------------------------
# Legacy ntfy unit tests
# ---------------------------------------------------------------------------


@pytest.fixture
def _recording(monkeypatch):
    """Install a recording httpx.post for the worker integration tests."""
    rec = _RecordingPost(200)
    monkeypatch.setattr(httpx, "post", rec)
    return rec


@pytest.fixture
def _notify_settings(secrets_set):
    """Enable ntfy for the integration tests."""
    secrets_set(ntfy_url="https://ntfy.sh/mill")
    # Return nothing — the fixture just sets up secrets


async def test_notifies_on_human_issue_approval(
    ctx, service, monkeypatch, _notify_settings, _recording
):
    """Worker-driven transition into human_issue_approval fires a notification."""

    class TriggerStage(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _t, _c):
            return Outcome(State.HUMAN_ISSUE_APPROVAL, "refined")

    monkeypatch.setitem(registry.STAGES, "refine", TriggerStage())
    t = service.create("trigger test")
    await process_ticket(t.id, ctx)

    assert service.get(t.id).state == State.HUMAN_ISSUE_APPROVAL
    assert len(_recording.calls) == 1


async def test_notifies_on_blocked(
    ctx, service, monkeypatch, _notify_settings, _recording
):
    """Worker-driven transition into blocked fires a notification."""

    class TriggerStage(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _t, _c):
            return Outcome(State.BLOCKED, "escalated")

    monkeypatch.setitem(registry.STAGES, "refine", TriggerStage())
    t = service.create("trigger test")
    await process_ticket(t.id, ctx)

    assert service.get(t.id).state == State.BLOCKED
    assert len(_recording.calls) == 1


async def test_notifies_on_human_mr_approval(
    ctx, service, monkeypatch, _notify_settings, _recording
):
    """Worker-driven transition into human_mr_approval fires a notification.
    The valid path is deliverable -> implement_complete -> human_mr_approval;
    mock the deliver and merge stages and pre-transition through draft -> ready -> deliverable."""

    # Mock refine and implement as no-ops so they don't interfere.
    class NoOp(Stage):
        def run(self, t, _c):
            return Outcome(t.state)

    for sn in list(registry.STAGES.keys()):
        if sn not in ("deliver", "merge"):
            s = NoOp()
            s.name = sn
            monkeypatch.setitem(registry.STAGES, sn, s)

    class MockDeliver(Stage):
        name = "deliver"
        input_state = State.DELIVERABLE

        def run(self, _t, _c):
            return Outcome(State.IMPLEMENT_COMPLETE, "PR opened")

    monkeypatch.setitem(registry.STAGES, "deliver", MockDeliver())

    class MockMerge(Stage):
        name = "merge"
        input_state = State.IMPLEMENT_COMPLETE

        def run(self, _t, _c):
            return Outcome(State.HUMAN_MR_APPROVAL, "gates passed")

    monkeypatch.setitem(registry.STAGES, "merge", MockMerge())
    t = service.create("in-review test")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.DELIVERABLE)
    await process_ticket(t.id, ctx)

    assert service.get(t.id).state == State.HUMAN_MR_APPROVAL
    assert len(_recording.calls) == 1


async def test_notifies_on_errored_from_stage(
    ctx, service, monkeypatch, _notify_settings, _recording
):
    """Worker-driven fatal stage exception transitions to BLOCKED and
    fires a notification."""

    class BoomStage(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _t, _c):
            raise RuntimeError("boom")

    monkeypatch.setitem(registry.STAGES, "refine", BoomStage())
    t = service.create("errored test")
    await process_ticket(t.id, ctx)

    assert service.get(t.id).state == State.BLOCKED
    assert len(_recording.calls) == 1


async def test_does_not_notify_on_ready(
    ctx, service, monkeypatch, _notify_settings, _recording
):
    """Worker-driven transition into ready must NOT fire a notification.
    Mock all downstream stages as no-ops so the chain stops immediately."""
    all_stages = list(registry.STAGES.keys())

    class NoOpFinish(Stage):
        def run(self, t, _c):
            return Outcome(t.state)

    for sn in all_stages:
        stage = NoOpFinish()
        stage.name = sn
        monkeypatch.setitem(registry.STAGES, sn, stage)

    class RefineStage(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _t, _c):
            return Outcome(State.READY, "refined")

    monkeypatch.setitem(registry.STAGES, "refine", RefineStage())
    t = service.create("non-trigger test")
    await process_ticket(t.id, ctx)

    assert service.get(t.id).state == State.READY
    assert len(_recording.calls) == 0


async def test_no_notification_on_noop(
    ctx, service, monkeypatch, _notify_settings, _recording
):
    """Poll-driven no-op (same-state Outcome) must NOT fire a notification."""

    class NoOpStage(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _t, _c):
            return Outcome(State.DRAFT, "waiting")

    monkeypatch.setitem(registry.STAGES, "refine", NoOpStage())
    t = service.create("noop test")
    await process_ticket(t.id, ctx)
    assert service.get(t.id).state is State.DRAFT
    assert len(_recording.calls) == 0


async def test_notification_exception_does_not_fail_worker(
    ctx, service, monkeypatch, _notify_settings, caplog
):
    """Even if the notification POST raises, the ticket still transitions."""
    rec = _RecordingPost(exc=ConnectionError("refused"))
    monkeypatch.setattr(httpx, "post", rec)

    class FailingNotifyStage(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _t, _c):
            return Outcome(State.HUMAN_ISSUE_APPROVAL, "should still work")

    monkeypatch.setitem(registry.STAGES, "refine", FailingNotifyStage())
    t = service.create("resilient test")
    await process_ticket(t.id, ctx)

    assert service.get(t.id).state == State.HUMAN_ISSUE_APPROVAL
    assert rec.calls
    assert "ntfy notification failed" in caplog.text


async def test_no_notification_when_url_unset_in_worker(
    ctx, service, monkeypatch, _notify_settings, _recording, secrets_set
):
    """When NTFY_URL is unset, the worker path still works but does not notify."""
    secrets_set(ntfy_url=None)

    class ApproveStage(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _t, _c):
            return Outcome(State.HUMAN_ISSUE_APPROVAL, "refined")

    monkeypatch.setitem(registry.STAGES, "refine", ApproveStage())
    t = service.create("no-url test")
    await process_ticket(t.id, ctx)

    assert service.get(t.id).state == State.HUMAN_ISSUE_APPROVAL
    assert len(_recording.calls) == 0
