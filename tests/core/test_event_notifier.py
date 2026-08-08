"""Unit tests for :mod:`robotsix_mill.core.event_notifier`.

Exercises the ``EventNotifier`` class directly: payload construction,
synchronous delivery, shared-secret header injection, and the
retry-and-drop behaviour when a subscriber is unreachable.
No real network calls — all HTTP is mocked via ``unittest.mock``.
"""

from __future__ import annotations

import json
from unittest import mock

import httpx

from robotsix_mill.core.event_notifier import EventNotifier, _build_payload, _deliver
from robotsix_mill.core.models import State, Ticket


def _make_ticket(
    ticket_id: str = "t-1",
    state: State = State.DRAFT,
    board_id: str = "test-board",
) -> Ticket:
    return Ticket(
        id=ticket_id,
        title="Test",
        workspace_path="/tmp/t-1",
        state=state,
        board_id=board_id,
    )


def _build_mock_response(status: int = 200) -> mock.MagicMock:
    resp = mock.MagicMock(spec=httpx.Response)
    resp.is_success = status < 400
    resp.status_code = status
    return resp


# ------------------------------------------------------------------
# Payload construction
# ------------------------------------------------------------------


def test_build_payload_shape() -> None:
    ticket = _make_ticket()
    payload = _build_payload(ticket, "draft")
    assert set(payload.keys()) == {
        "ticket_id",
        "board_id",
        "old_state",
        "new_state",
        "timestamp",
    }
    assert payload["ticket_id"] == "t-1"
    assert payload["board_id"] == "test-board"
    assert payload["old_state"] == "draft"
    assert payload["new_state"] == "draft"  # same object
    assert payload["timestamp"].endswith("+00:00") or payload["timestamp"].endswith("Z")


# ------------------------------------------------------------------
# Synchronous delivery
# ------------------------------------------------------------------


def test_notify_sync_posts_to_all_subscribers() -> None:
    notifier = EventNotifier(
        subscriber_urls=["http://sub1/events", "http://sub2/events"],
    )
    with mock.patch(
        "robotsix_mill.core.event_notifier._deliver", autospec=True
    ) as mock_deliver:
        notifier.notify_sync(_make_ticket(), "draft")
        assert mock_deliver.call_count == 2


def test_notify_sync_injects_shared_secret_header() -> None:
    notifier = EventNotifier(
        subscriber_urls=["http://sub/events"],
        shared_secret="s3cret",
    )
    with mock.patch(
        "robotsix_mill.core.event_notifier._deliver", autospec=True
    ) as mock_deliver:
        notifier.notify_sync(_make_ticket(), "draft")
        _, _, headers = mock_deliver.call_args[0]
        assert headers["X-Mill-Event-Secret"] == "s3cret"


def test_notify_sync_omits_secret_header_when_none() -> None:
    notifier = EventNotifier(subscriber_urls=["http://sub/events"])
    with mock.patch(
        "robotsix_mill.core.event_notifier._deliver", autospec=True
    ) as mock_deliver:
        notifier.notify_sync(_make_ticket(), "draft")
        _, _, headers = mock_deliver.call_args[0]
        assert "X-Mill-Event-Secret" not in headers


def test_notify_sync_no_subscribers_is_noop() -> None:
    notifier = EventNotifier(subscriber_urls=[])
    with mock.patch(
        "robotsix_mill.core.event_notifier._deliver", autospec=True
    ) as mock_deliver:
        notifier.notify_sync(_make_ticket(), "draft")
        mock_deliver.assert_not_called()


# ------------------------------------------------------------------
# _deliver (retry logic)
# ------------------------------------------------------------------


def test_deliver_success_on_first_attempt() -> None:
    mock_client = mock.MagicMock()
    mock_resp = _build_mock_response(200)
    mock_client.__enter__.return_value.post.return_value = mock_resp

    with mock.patch(
        "robotsix_mill.core.event_notifier.httpx.Client", return_value=mock_client
    ):
        _deliver(
            "http://sub/events",
            '{"ticket_id":"t-1"}',
            {"Content-Type": "application/json"},
        )

    mock_client.__enter__.return_value.post.assert_called_once()


def test_deliver_retries_on_500_then_succeeds() -> None:
    mock_client = mock.MagicMock()
    mock_client.__enter__.return_value.post.side_effect = [
        _build_mock_response(500),
        _build_mock_response(500),
        _build_mock_response(200),
    ]

    with mock.patch(
        "robotsix_mill.core.event_notifier.httpx.Client", return_value=mock_client
    ):
        _deliver(
            "http://sub/events",
            '{"ticket_id":"t-1"}',
            {"Content-Type": "application/json"},
        )

    assert mock_client.__enter__.return_value.post.call_count == 3


def test_deliver_drops_after_max_attempts() -> None:
    mock_client = mock.MagicMock()
    mock_client.__enter__.return_value.post.return_value = _build_mock_response(500)

    with mock.patch(
        "robotsix_mill.core.event_notifier.httpx.Client", return_value=mock_client
    ):
        _deliver(
            "http://sub/events",
            '{"ticket_id":"t-1"}',
            {"Content-Type": "application/json"},
        )

    # 3 attempts, all fail, no exception raised.
    assert mock_client.__enter__.return_value.post.call_count == 3


def test_deliver_retries_on_connection_error() -> None:
    mock_client = mock.MagicMock()
    mock_client.__enter__.return_value.post.side_effect = [
        httpx.ConnectError("connection refused"),
        _build_mock_response(200),
    ]

    with mock.patch(
        "robotsix_mill.core.event_notifier.httpx.Client", return_value=mock_client
    ):
        _deliver(
            "http://sub/events",
            '{"ticket_id":"t-1"}',
            {"Content-Type": "application/json"},
        )

    assert mock_client.__enter__.return_value.post.call_count == 2
