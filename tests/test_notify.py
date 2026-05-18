"""Hermetic tests for the ntfy notification subsystem."""

from __future__ import annotations

import httpx
import pytest

from robotsix_mill.core.states import State
from robotsix_mill.notify import send_notification, _TRIGGER_STATES
from robotsix_mill.runtime.worker import process_ticket
from robotsix_mill.stages import Outcome, StageContext
from robotsix_mill.stages import registry
from robotsix_mill.stages.base import Stage


class _RecordingPost:
    """Drop-in replacement for ``httpx.post`` that records calls and
    returns a configurable response."""

    def __init__(self, status_code: int = 200, exc: Exception | None = None):
        self.calls: list[dict] = []
        self._status = status_code
        self._exc = exc

    def __call__(self, url, *, headers=None, content=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "content": content, "timeout": timeout})
        if self._exc is not None:
            raise self._exc
        return _FakeResponse(self._status)


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error", request=None, response=self  # type: ignore[arg-type]
            )


@pytest.fixture
def ctx(settings, service):
    return StageContext(settings=settings, service=service)


# ---------------------------------------------------------------------------
# send_notification unit tests
# ---------------------------------------------------------------------------


def test_noop_when_url_unset(settings, service):
    """When NTFY_URL is None / empty, send_notification returns immediately."""
    settings.ntfy_url = None
    t = service.create("x")
    send_notification(t, State.ERRORED, "boom", settings)


def test_noop_when_url_empty(settings, service):
    """Empty string is treated same as None."""
    settings.ntfy_url = ""
    t = service.create("x")
    send_notification(t, State.ERRORED, "boom", settings)


def test_posts_to_url(settings, service, monkeypatch):
    """Happy path: a POST is made with the correct headers and body."""
    settings.ntfy_url = "https://ntfy.sh/test"
    settings.ntfy_token = None
    rec = _RecordingPost(200)
    monkeypatch.setattr(httpx, "post", rec)

    t = service.create("Add feature")
    send_notification(t, State.AWAITING_APPROVAL, "refined spec", settings)

    assert len(rec.calls) == 1
    c = rec.calls[0]
    assert c["url"] == "https://ntfy.sh/test"
    assert c["headers"]["X-Title"] == "mill: awaiting_approval - Add feature"
    assert c["headers"]["Content-Type"] == "text/plain"
    assert "Authorization" not in c["headers"]
    assert f"Ticket: {t.id}" in c["content"]
    assert "State: awaiting_approval" in c["content"]
    assert "Note: refined spec" in c["content"]
    assert "Board: http://127.0.0.1:8077" in c["content"]
    assert c["timeout"] is not None


def test_xtitle_header_is_ascii_safe(settings, service, monkeypatch):
    """Regression: an em-dash (or any non-ASCII ticket title) in the
    X-Title header made httpx raise UnicodeEncodeError, silently
    breaking EVERY notification. The header must be ASCII-encodable."""
    settings.ntfy_url = "https://ntfy.sh/test"
    settings.ntfy_token = None
    rec = _RecordingPost(200)
    monkeypatch.setattr(httpx, "post", rec)

    t = service.create("Café — naïve ☕ build")  # non-ASCII title
    send_notification(t, State.IN_REVIEW, "PR opened", settings)  # must not raise

    assert len(rec.calls) == 1
    xt = rec.calls[0]["headers"]["X-Title"]
    xt.encode("ascii")          # the actual failure mode — must not raise
    assert "—" not in xt        # em-dash gone
    assert xt.startswith("mill: in_review - ")


def test_note_none_renders_placeholder(settings, service, monkeypatch):
    """A None note becomes '(none)' in the body."""
    settings.ntfy_url = "https://ntfy.sh/test"
    rec = _RecordingPost(200)
    monkeypatch.setattr(httpx, "post", rec)

    t = service.create("x")
    send_notification(t, State.BLOCKED, None, settings)
    assert "Note: (none)" in rec.calls[0]["content"]


def test_token_sent_as_bearer(settings, service, monkeypatch):
    """NTFY_TOKEN is sent as an Authorization: Bearer header."""
    settings.ntfy_url = "https://ntfy.sh/test"
    settings.ntfy_token = "tk_secret"
    rec = _RecordingPost(200)
    monkeypatch.setattr(httpx, "post", rec)

    t = service.create("x")
    send_notification(t, State.IN_REVIEW, "PR opened", settings)
    assert rec.calls[0]["headers"]["Authorization"] == "Bearer tk_secret"


def test_network_error_is_caught(settings, service, monkeypatch, caplog):
    """A POST exception does not propagate — it logs a warning and returns."""
    settings.ntfy_url = "https://ntfy.sh/test"
    rec = _RecordingPost(exc=ConnectionError("refused"))
    monkeypatch.setattr(httpx, "post", rec)

    t = service.create("x")
    send_notification(t, State.ERRORED, "fail", settings)
    assert rec.calls
    assert "ntfy notification failed" in caplog.text


def test_non_2xx_is_caught(settings, service, monkeypatch, caplog):
    """A 500 response is treated as a failure and logged, not raised."""
    settings.ntfy_url = "https://ntfy.sh/test"
    rec = _RecordingPost(status_code=500)
    monkeypatch.setattr(httpx, "post", rec)

    t = service.create("x")
    send_notification(t, State.BLOCKED, "stuck", settings)
    assert rec.calls
    assert "ntfy notification failed" in caplog.text


# ---------------------------------------------------------------------------
# Worker integration tests (via process_ticket)
# ---------------------------------------------------------------------------


@pytest.fixture
def _recording(monkeypatch):
    """Install a recording httpx.post for the worker integration tests."""
    rec = _RecordingPost(200)
    monkeypatch.setattr(httpx, "post", rec)
    return rec


@pytest.fixture
def _notify_settings(settings):
    """Enable ntfy for the integration tests."""
    settings.ntfy_url = "https://ntfy.sh/mill"
    return settings


async def test_notifies_on_awaiting_approval(ctx, service, monkeypatch, _notify_settings, _recording):
    """Worker-driven transition into awaiting_approval fires a notification."""
    class TriggerStage(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _t, _c):
            return Outcome(State.AWAITING_APPROVAL, "refined")

    monkeypatch.setitem(registry.STAGES, "refine", TriggerStage())
    t = service.create("trigger test")
    await process_ticket(t.id, ctx)

    assert service.get(t.id).state == State.AWAITING_APPROVAL
    assert len(_recording.calls) == 1


async def test_notifies_on_blocked(ctx, service, monkeypatch, _notify_settings, _recording):
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


async def test_notifies_on_in_review(ctx, service, monkeypatch, _notify_settings, _recording):
    """Worker-driven transition into in_review fires a notification.
    The valid path is deliverable -> in_review; mock the deliver stage
    and pre-transition through draft -> ready -> deliverable."""
    # Mock refine and implement as no-ops so they don't interfere.
    class NoOp(Stage):
        def run(self, t, _c):
            return Outcome(t.state)

    for sn in list(registry.STAGES.keys()):
        if sn not in ("deliver",):
            s = NoOp()
            s.name = sn
            monkeypatch.setitem(registry.STAGES, sn, s)

    class DeliverStage(Stage):
        name = "deliver"
        input_state = State.DELIVERABLE

        def run(self, _t, _c):
            return Outcome(State.IN_REVIEW, "PR opened")

    monkeypatch.setitem(registry.STAGES, "deliver", DeliverStage())
    t = service.create("in-review test")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.DELIVERABLE)
    await process_ticket(t.id, ctx)

    assert service.get(t.id).state == State.IN_REVIEW
    assert len(_recording.calls) == 1


async def test_notifies_on_errored_from_stage(ctx, service, monkeypatch, _notify_settings, _recording):
    """Worker-driven transition into errored fires a notification (stage
    raises exception -> worker transitions to errored)."""
    class BoomStage(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _t, _c):
            raise RuntimeError("boom")

    monkeypatch.setitem(registry.STAGES, "refine", BoomStage())
    t = service.create("errored test")
    await process_ticket(t.id, ctx)

    assert service.get(t.id).state == State.ERRORED
    assert len(_recording.calls) == 1


async def test_does_not_notify_on_ready(ctx, service, monkeypatch, _notify_settings, _recording):
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
            return Outcome(State.AWAITING_APPROVAL, "should still work")

    monkeypatch.setitem(registry.STAGES, "refine", FailingNotifyStage())
    t = service.create("resilient test")
    await process_ticket(t.id, ctx)

    assert service.get(t.id).state == State.AWAITING_APPROVAL
    assert rec.calls
    assert "ntfy notification failed" in caplog.text


async def test_no_notification_when_url_unset_in_worker(
    ctx, service, monkeypatch, _notify_settings, _recording
):
    """When NTFY_URL is unset, the worker path still works but does not notify."""
    _notify_settings.ntfy_url = None

    class ApproveStage(Stage):
        name = "refine"
        input_state = State.DRAFT

        def run(self, _t, _c):
            return Outcome(State.AWAITING_APPROVAL, "refined")

    monkeypatch.setitem(registry.STAGES, "refine", ApproveStage())
    t = service.create("no-url test")
    await process_ticket(t.id, ctx)

    assert service.get(t.id).state == State.AWAITING_APPROVAL
    assert len(_recording.calls) == 0
