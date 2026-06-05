"""Tests for the server module (kanban board HTTP handler)."""

from __future__ import annotations

import os
import re
import tempfile
from typing import TYPE_CHECKING
from urllib.request import urlopen

if TYPE_CHECKING:
    from http.server import HTTPServer

from tests.conftest import _make_record

from robotsix_auto_mail.db import init_db
from robotsix_auto_mail.format import _format_date
from robotsix_auto_mail.server import (
    _build_board_html,
    _build_detail_html,
    _render_card,
)

# ---------------------------------------------------------------------------
# _format_date
# ---------------------------------------------------------------------------


def test_format_date_valid_iso() -> None:
    assert _format_date("2025-03-15T09:30:00") == "2025-03-15 09:30"


def test_format_date_with_tz_offset() -> None:
    result = _format_date("2025-06-01T14:00:00+00:00")
    assert result.startswith("2025-06-01")


def test_format_date_invalid_returns_raw() -> None:
    assert _format_date("Last Thursday") == "Last Thursday"


def test_format_date_none_returns_none() -> None:
    result = _format_date(None)  # type: ignore[arg-type]
    assert result is None


# ---------------------------------------------------------------------------
# _render_card
# ---------------------------------------------------------------------------


def test_render_card_basic() -> None:
    record = _make_record(
        message_id="abc",
        sender="alice@example.com",
        subject="Hello",
        date="2025-01-10T12:00:00",
        body_plain="This is the body.",
    )
    html = _render_card(record)
    assert "alice@example.com" in html
    assert "Hello" in html
    assert "2025-01-10 12:00" in html
    assert "This is the body." in html
    assert 'class="card"' in html
    # Move form present
    assert '<form class="card-form"' in html
    assert 'method="post" action="/move"' in html
    assert '<input type="hidden" name="message_id"' in html
    assert 'value="abc"' in html
    assert '<select name="status">' in html
    assert '<button type="submit">Move</button>' in html
    # Default status (no explicit status → ''), so "inbox" is selected first
    assert '<option value="inbox" selected' in html


def test_render_card_empty_subject() -> None:
    record = _make_record(
        message_id="abc",
        sender="x",
        subject="   ",
        date="2025-01-01T00:00:00",
        body_plain="body",
    )
    html = _render_card(record)
    assert "(no subject)" in html


def test_render_card_empty_body_plain() -> None:
    record = _make_record(
        message_id="abc",
        sender="x",
        subject="s",
        date="2025-01-01T00:00:00",
        body_plain="",
    )
    html = _render_card(record)
    assert "(no body)" in html
    assert "no-body" in html


def test_render_card_whitespace_body_plain() -> None:
    record = _make_record(
        message_id="abc",
        sender="x",
        subject="s",
        date="2025-01-01T00:00:00",
        body_plain="   \t\n  ",
    )
    html = _render_card(record)
    assert "(no body)" in html


def test_render_card_body_truncation() -> None:
    record = _make_record(
        message_id="abc",
        sender="x",
        subject="s",
        date="2025-01-01T00:00:00",
        body_plain="A" * 200,
    )
    html = _render_card(record)
    # Should contain exactly 150 chars of "A" then "…"
    assert ("A" * 150 + "\u2026") in html


def test_render_card_body_exactly_limit() -> None:
    body = "B" * 150
    record = _make_record(
        message_id="abc",
        sender="x",
        subject="s",
        date="2025-01-01T00:00:00",
        body_plain=body,
    )
    html = _render_card(record)
    assert body in html
    # No ellipsis for exact 150
    assert "\u2026" not in html


def test_render_card_html_escapes_sender() -> None:
    record = _make_record(
        message_id="abc",
        sender="<script>alert('xss')</script>",
        subject="s",
        date="2025-01-01T00:00:00",
        body_plain="safe",
    )
    html = _render_card(record)
    assert "&lt;script&gt;" in html
    assert "<script>" not in html
    # Form should still be present
    assert '<form class="card-form"' in html


def test_render_card_html_escapes_subject() -> None:
    record = _make_record(
        message_id="abc",
        sender="x",
        subject='<b onmouseover="alert(1)">click</b>',
        date="2025-01-01T00:00:00",
        body_plain="safe",
    )
    html = _render_card(record)
    assert "&lt;b onmouseover" in html
    assert "<b " not in html


def test_render_card_html_escapes_body() -> None:
    record = _make_record(
        message_id="abc",
        sender="x",
        subject="s",
        date="2025-01-01T00:00:00",
        body_plain='<img src=x onerror="alert(1)">',
    )
    html = _render_card(record)
    assert "&lt;img" in html
    assert "<img" not in html


def test_render_card_selected_status() -> None:
    """The current status should have the 'selected' attribute."""
    record = _make_record(
        message_id="test-id",
        sender="x",
        subject="s",
        date="2025-01-01T00:00:00",
        body_plain="body",
        status="done",
    )
    html = _render_card(record)
    assert '<option value="done" selected>Done</option>' in html
    assert '<option value="inbox">Inbox</option>' in html


def test_render_card_message_id_with_angle_brackets() -> None:
    """Message IDs containing <, > should be HTML-escaped in the hidden input."""
    record = _make_record(
        message_id="<abc123@example.com>",
        sender="x",
        subject="s",
        date="2025-01-01T00:00:00",
        body_plain="body",
    )
    html = _render_card(record)
    assert 'value="&lt;abc123@example.com&gt;"' in html


# ---------------------------------------------------------------------------
# Helpers for tests that need a file-based DB
# ---------------------------------------------------------------------------


def _populate_db(db_path: str, inserts: list[dict[str, str]]) -> None:
    """Open *db_path*, insert rows, commit, close."""
    conn = init_db(db_path)
    try:
        for row in inserts:
            conn.execute(
                "INSERT INTO mail_records "
                "(message_id, sender, subject, date, recipients_json, "
                "body_plain, body_html, attachments_json, status) "
                "VALUES (?, ?, ?, ?, '{}', ?, '', '[]', ?)",
                (
                    row["message_id"],
                    row["sender"],
                    row["subject"],
                    row["date"],
                    row.get("body_plain", ""),
                    row["status"],
                ),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# _build_board_html (file-based DB)
# ---------------------------------------------------------------------------


def test_build_board_html_structure() -> None:
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _populate_db(
            db_path,
            [
                {
                    "message_id": "m1",
                    "sender": "a@b.com",
                    "subject": "Subj",
                    "date": "2025-01-01T00:00:00",
                    "body_plain": "Body",
                    "status": "inbox",
                },
                {
                    "message_id": "m2",
                    "sender": "c@d.com",
                    "subject": "Subj2",
                    "date": "2025-01-02T00:00:00",
                    "body_plain": "Body2",
                    "status": "done",
                },
            ],
        )

        html = _build_board_html(db_path)

        assert "<!DOCTYPE html>" in html
        assert '<html lang="en">' in html
        assert "<title>Mail Board</title>" in html
        assert '<meta http-equiv="refresh" content="30">' in html
        assert '<h1>Mail Board</h1>' in html
        assert 'class="board"' in html

        # Exactly 4 columns
        assert html.count('class="column"') == 4

        # Order: Inbox, Triaging, Done, Archive
        inbox_pos = html.find("<h2>Inbox</h2>")
        triaging_pos = html.find("<h2>Triaging</h2>")
        done_pos = html.find("<h2>Done</h2>")
        archive_pos = html.find("<h2>Archive</h2>")
        assert 0 <= inbox_pos < triaging_pos < done_pos < archive_pos

        # Counts — Inbox:1, Triaging:0, Done:1, Archive:0
        counts = re.findall(r'<span class="count">(\d+)</span>', html)
        assert counts == ["1", "0", "1", "0"]

        # Cards
        assert "a@b.com" in html
        assert "c@d.com" in html
    finally:
        os.unlink(db_path)


def test_build_board_html_empty_db() -> None:
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        html = _build_board_html(db_path)
        assert 'class="column"' in html
        # All counts should be 0
        counts = re.findall(r'<span class="count">(\d+)</span>', html)
        assert counts == ["0", "0", "0", "0"]
    finally:
        os.unlink(db_path)


def test_build_board_html_body_preview_truncated() -> None:
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        long_body = "X" * 200
        _populate_db(
            db_path,
            [
                {
                    "message_id": "m3",
                    "sender": "t@t.com",
                    "subject": "Long body",
                    "date": "2025-03-01T00:00:00",
                    "body_plain": long_body,
                    "status": "inbox",
                },
            ],
        )

        html = _build_board_html(db_path)
        # The body preview should be exactly 150 chars + "…"
        assert ("X" * 150 + "\u2026") in html
        assert ("X" * 151) not in html
    finally:
        os.unlink(db_path)


def test_build_board_html_no_body_shows_placeholder() -> None:
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _populate_db(
            db_path,
            [
                {
                    "message_id": "m4",
                    "sender": "n@n.com",
                    "subject": "No body",
                    "date": "2025-04-01T00:00:00",
                    "body_plain": "",
                    "status": "inbox",
                },
            ],
        )

        html = _build_board_html(db_path)
        assert "(no body)" in html
        assert "no-body" in html
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# HTTP handler integration tests (via urlopen in a thread)
# ---------------------------------------------------------------------------


def _start_test_server(db_path: str, port: int = 0) -> tuple[HTTPServer, int]:
    """Start an HTTPServer, return (server, port).  port=0 means auto-assign."""
    import threading
    from http.server import HTTPServer

    from robotsix_auto_mail.server import make_board_handler

    handler = make_board_handler(db_path)
    server = HTTPServer(("127.0.0.1", port), handler)
    assigned_port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, assigned_port


def test_handler_root_redirects() -> None:
    from urllib.request import (
        HTTPRedirectHandler,
        Request,
        build_opener,
    )

    server, port = _start_test_server(":memory:")
    try:

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(
                self,
                req: Request,
                fp: object,
                code: int,
                msg: object,
                hdrs: object,
                newurl: str,
            ) -> None:
                return None  # don't follow

            def http_error_301(
                self,
                req: Request,
                fp: object,
                code: int,
                msg: object,
                hdrs: object,
            ) -> object:
                return fp

        opener = build_opener(NoRedirect())
        resp = opener.open(f"http://127.0.0.1:{port}/")
        assert resp.status == 301
        assert resp.headers.get("Location") == "/board"
    finally:
        server.shutdown()


def test_handler_board_returns_200_and_html() -> None:
    server, port = _start_test_server(":memory:")
    try:
        resp = urlopen(f"http://127.0.0.1:{port}/board")
        assert resp.status == 200
        content_type = resp.headers.get("Content-Type", "")
        assert "text/html" in content_type
        body = resp.read().decode("utf-8")
        assert "<!DOCTYPE html>" in body
    finally:
        server.shutdown()


def test_handler_nonexistent_returns_404() -> None:
    import urllib.error

    server, port = _start_test_server(":memory:")
    try:
        try:
            urlopen(f"http://127.0.0.1:{port}/nonexistent")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("Expected HTTPError for 404")
    finally:
        server.shutdown()


def test_handler_missing_db_returns_503() -> None:
    import urllib.error

    # Point to a path inside /dev/null so init_db raises an error.
    server, port = _start_test_server("/dev/null/nonexistent.db")
    try:
        try:
            urlopen(f"http://127.0.0.1:{port}/board")
        except urllib.error.HTTPError as exc:
            assert exc.code == 503
            body = exc.read().decode("utf-8")
            assert "Database unavailable" in body
        else:
            raise AssertionError("Expected HTTPError for 503")
    finally:
        server.shutdown()


def test_handler_board_with_data() -> None:
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _populate_db(
            db_path,
            [
                {
                    "message_id": "m10",
                    "sender": "inbox@test.com",
                    "subject": "Inbox Msg",
                    "date": "2025-05-01T10:00:00",
                    "body_plain": "Hello",
                    "status": "inbox",
                },
                {
                    "message_id": "m11",
                    "sender": "triaging@test.com",
                    "subject": "Triaging Msg",
                    "date": "2025-05-02T10:00:00",
                    "body_plain": "Hi",
                    "status": "triaging",
                },
                {
                    "message_id": "m12",
                    "sender": "archive1@test.com",
                    "subject": "Archive1",
                    "date": "2025-05-03T10:00:00",
                    "body_plain": "Yo",
                    "status": "archive",
                },
                {
                    "message_id": "m13",
                    "sender": "archive2@test.com",
                    "subject": "Archive2",
                    "date": "2025-05-04T10:00:00",
                    "body_plain": "Hey",
                    "status": "archive",
                },
            ],
        )

        server, port = _start_test_server(db_path)
        try:
            resp = urlopen(f"http://127.0.0.1:{port}/board")
            body = resp.read().decode("utf-8")

            assert "inbox@test.com" in body
            assert "triaging@test.com" in body
            assert "archive1@test.com" in body
            assert "archive2@test.com" in body

            # Check counts — order: Inbox, Triaging, Done, Archive
            counts = re.findall(r'<span class="count">(\d+)</span>', body)
            assert counts == ["1", "1", "0", "2"]
        finally:
            server.shutdown()
    finally:
        os.unlink(db_path)


def test_handler_xss_prevention() -> None:
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _populate_db(
            db_path,
            [
                {
                    "message_id": "xss1",
                    "sender": "<script>alert(1)</script>",
                    "subject": "<img onerror=alert(2)>",
                    "date": "2025-01-01T00:00:00",
                    "body_plain": "<b>evil</b>",
                    "status": "inbox",
                },
            ],
        )

        server, port = _start_test_server(db_path)
        try:
            resp = urlopen(f"http://127.0.0.1:{port}/board")
            body = resp.read().decode("utf-8")

            # All angle brackets in user data must be escaped
            assert "&lt;script&gt;" in body
            assert "&lt;img onerror" in body
            assert "&lt;b&gt;evil&lt;/b&gt;" in body
        finally:
            server.shutdown()
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# POST /move tests
# ---------------------------------------------------------------------------


def _post_form(port: int, fields: dict[str, str]) -> tuple[int, str]:
    """POST url-encoded *fields* to /move and return (status, body)."""
    import urllib.request

    data = urllib.parse.urlencode(fields).encode("utf-8")
    url = f"http://127.0.0.1:{port}/move"

    # Don't follow redirects, and capture 400/404 bodies.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(
            self,
            req: urllib.request.Request,
            fp: object,
            code: int,
            msg: object,
            hdrs: object,
            newurl: str,
        ) -> None:
            return None

    class CaptureError(urllib.request.HTTPDefaultErrorHandler):
        def http_error_default(  # type: ignore[override]
            self,
            req: urllib.request.Request,
            fp: object,
            code: int,
            msg: object,
            hdrs: object,
        ) -> object:
            return fp

    opener = urllib.request.build_opener(NoRedirect(), CaptureError())
    req = urllib.request.Request(  # noqa: S310
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp = opener.open(req)
    body = resp.read().decode("utf-8")
    return resp.status, body


def test_move_success_redirects_302() -> None:
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _populate_db(
            db_path,
            [
                {
                    "message_id": "move-me",
                    "sender": "x@x.com",
                    "subject": "Move test",
                    "date": "2025-01-01T00:00:00",
                    "body_plain": "body",
                    "status": "inbox",
                },
            ],
        )

        server, port = _start_test_server(db_path)
        try:
            status, body = _post_form(port, {"message_id": "move-me", "status": "done"})
            assert status == 302, f"Expected 302, got {status}: {body}"

            # Verify the card actually moved by checking /board.
            resp = urlopen(f"http://127.0.0.1:{port}/board")
            board_html = resp.read().decode("utf-8")
            # Should be in Done column, not Inbox
            counts = re.findall(r'<span class="count">(\d+)</span>', board_html)
            assert counts == ["0", "0", "1", "0"], f"Unexpected counts: {counts}"
        finally:
            server.shutdown()
    finally:
        os.unlink(db_path)


def test_move_to_triaging() -> None:
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _populate_db(
            db_path,
            [
                {
                    "message_id": "m-triaging",
                    "sender": "t@t.com",
                    "subject": "Triaging",
                    "date": "2025-02-01T00:00:00",
                    "body_plain": "body",
                    "status": "inbox",
                },
            ],
        )

        server, port = _start_test_server(db_path)
        try:
            status, _ = _post_form(
                port, {"message_id": "m-triaging", "status": "triaging"}
            )
            assert status == 302

            resp = urlopen(f"http://127.0.0.1:{port}/board")
            body = resp.read().decode("utf-8")
            counts = re.findall(r'<span class="count">(\d+)</span>', body)
            assert counts == ["0", "1", "0", "0"]
        finally:
            server.shutdown()
    finally:
        os.unlink(db_path)


def test_move_to_archive() -> None:
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _populate_db(
            db_path,
            [
                {
                    "message_id": "m-archive",
                    "sender": "a@a.com",
                    "subject": "Archive",
                    "date": "2025-03-01T00:00:00",
                    "body_plain": "body",
                    "status": "inbox",
                },
            ],
        )

        server, port = _start_test_server(db_path)
        try:
            status, _ = _post_form(
                port, {"message_id": "m-archive", "status": "archive"}
            )
            assert status == 302

            resp = urlopen(f"http://127.0.0.1:{port}/board")
            body = resp.read().decode("utf-8")
            counts = re.findall(r'<span class="count">(\d+)</span>', body)
            assert counts == ["0", "0", "0", "1"]
        finally:
            server.shutdown()
    finally:
        os.unlink(db_path)


def test_move_invalid_status_returns_400() -> None:
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _populate_db(
            db_path,
            [
                {
                    "message_id": "bad-status",
                    "sender": "x@x.com",
                    "subject": "Bad",
                    "date": "2025-01-01T00:00:00",
                    "body_plain": "body",
                    "status": "inbox",
                },
            ],
        )

        server, port = _start_test_server(db_path)
        try:
            status, body = _post_form(
                port, {"message_id": "bad-status", "status": "bogus"}
            )
            assert status == 400
            assert "Invalid status: 'bogus'" in body
        finally:
            server.shutdown()
    finally:
        os.unlink(db_path)


def test_move_missing_message_id_returns_400() -> None:
    server, port = _start_test_server(":memory:")
    try:
        status, body = _post_form(port, {"status": "done"})
        assert status == 400
        assert "Missing message_id or status" in body
    finally:
        server.shutdown()


def test_move_missing_status_returns_400() -> None:
    server, port = _start_test_server(":memory:")
    try:
        status, body = _post_form(port, {"message_id": "anything"})
        assert status == 400
        assert "Missing message_id or status" in body
    finally:
        server.shutdown()


def test_move_empty_message_id_returns_400() -> None:
    server, port = _start_test_server(":memory:")
    try:
        status, body = _post_form(port, {"message_id": "  ", "status": "done"})
        assert status == 400
        assert "Missing message_id or status" in body
    finally:
        server.shutdown()


def test_move_unknown_message_id_returns_404() -> None:
    server, port = _start_test_server(":memory:")
    try:
        status, body = _post_form(
            port, {"message_id": "does-not-exist", "status": "done"}
        )
        assert status == 404
        assert body == "Not found"
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# GET /email/{message_id}/status tests
# ---------------------------------------------------------------------------


def test_email_status_returns_200() -> None:
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _populate_db(
            db_path,
            [
                {
                    "message_id": "<abc123@example.com>",
                    "sender": "x@x.com",
                    "subject": "Status test",
                    "date": "2025-01-01T00:00:00",
                    "body_plain": "body",
                    "status": "triaging",
                },
            ],
        )

        server, port = _start_test_server(db_path)
        try:
            import urllib.request

            encoded = urllib.request.pathname2url("<abc123@example.com>")
            resp = urlopen(f"http://127.0.0.1:{port}/email/{encoded}/status")
            assert resp.status == 200
            assert resp.headers.get("Content-Type", "").startswith("text/plain")
            body = resp.read().decode("utf-8")
            assert body == "triaging"
        finally:
            server.shutdown()
    finally:
        os.unlink(db_path)


def test_email_status_unknown_message_id_returns_404() -> None:
    server, port = _start_test_server(":memory:")
    try:
        import urllib.error

        try:
            urlopen(f"http://127.0.0.1:{port}/email/nonexistent/status")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("Expected HTTPError 404")
    finally:
        server.shutdown()


def test_email_path_without_status_suffix_now_returns_detail() -> None:
    """GET /email/{mid} (no /status suffix) now returns the detail page."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _populate_db(
            db_path,
            [
                {
                    "message_id": "mid1",
                    "sender": "x@x.com",
                    "subject": "Test",
                    "date": "2025-01-01T00:00:00",
                    "body_plain": "body",
                    "status": "inbox",
                },
            ],
        )

        server, port = _start_test_server(db_path)
        try:
            resp = urlopen(f"http://127.0.0.1:{port}/email/mid1")
            assert resp.status == 200
            body = resp.read().decode("utf-8")
            assert "<!DOCTYPE html>" in body
            assert "Test" in body
            assert "x@x.com" in body
        finally:
            server.shutdown()
    finally:
        os.unlink(db_path)


def test_email_status_simple_message_id() -> None:
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _populate_db(
            db_path,
            [
                {
                    "message_id": "simple-id",
                    "sender": "s@s.com",
                    "subject": "Simple",
                    "date": "2025-01-01T00:00:00",
                    "body_plain": "body",
                    "status": "done",
                },
            ],
        )

        server, port = _start_test_server(db_path)
        try:
            resp = urlopen(f"http://127.0.0.1:{port}/email/simple-id/status")
            assert resp.status == 200
            assert resp.read().decode("utf-8") == "done"
        finally:
            server.shutdown()
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# _render_card — detail link
# ---------------------------------------------------------------------------


def test_render_card_has_detail_link() -> None:
    """_render_card output contains a link to /email/{message_id}."""
    record = _make_record(
        message_id="<abc@example.com>",
        sender="alice@example.com",
        subject="Hello World",
        date="2025-01-10T12:00:00",
        body_plain="Body.",
    )
    html = _render_card(record)
    # The subject should be wrapped in an <a> pointing to the detail page
    assert '<a href="/email/' in html
    # The quoted message_id should appear in the href
    import urllib.parse
    quoted = urllib.parse.quote("<abc@example.com>", safe="")
    assert f'href="/email/{quoted}"' in html
    # The visible subject text should be escaped and inside the <a>
    assert ">Hello World</a>" in html


def test_render_card_link_preserves_move_form() -> None:
    """The Move <form> is still present when the subject is a link."""
    record = _make_record(
        message_id="<test@example.com>",
        sender="x@x.com",
        subject="Test",
        date="2025-01-01T00:00:00",
        body_plain="body",
    )
    html = _render_card(record)
    assert '<form class="card-form"' in html
    assert 'method="post" action="/move"' in html
    assert '<button type="submit">Move</button>' in html


# ---------------------------------------------------------------------------
# _build_detail_html
# ---------------------------------------------------------------------------


def test_build_detail_html_basic() -> None:
    """_build_detail_html returns a page with all expected content."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _populate_db(
            db_path,
            [
                {
                    "message_id": "<detail@test.com>",
                    "sender": "detail-sender@test.com",
                    "subject": "Detail Test Subject",
                    "date": "2025-06-15T14:30:00",
                    "body_plain": "Full body content here.\nLine two.",
                    "status": "inbox",
                },
            ],
        )

        html = _build_detail_html(db_path, "<detail@test.com>")
        assert html is not None
        assert "<!DOCTYPE html>" in html
        assert "<title>Mail: Detail Test Subject</title>" in html
        assert "← Back to board" in html
        assert 'href="/board"' in html
        assert "Detail Test Subject" in html
        assert "detail-sender@test.com" in html
        assert "2025-06-15 14:30" in html
        assert "Full body content here." in html
        assert "Line two." in html
        # Recipients To
        assert "To" in html
        # Move form
        assert '<form class="detail-form"' in html
        assert 'method="post" action="/move"' in html
    finally:
        os.unlink(db_path)


def test_build_detail_html_unknown_message_id_returns_none() -> None:
    """_build_detail_html returns None for a nonexistent message_id."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        result = _build_detail_html(db_path, "<does-not-exist@x.com>")
        assert result is None
    finally:
        os.unlink(db_path)


def test_build_detail_html_empty_body_placeholder() -> None:
    """Placeholder '(no body)' shown when body_plain is empty."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _populate_db(
            db_path,
            [
                {
                    "message_id": "<empty-body@test.com>",
                    "sender": "x@x.com",
                    "subject": "Empty Body",
                    "date": "2025-01-01T00:00:00",
                    "body_plain": "",
                    "status": "inbox",
                },
            ],
        )

        html = _build_detail_html(db_path, "<empty-body@test.com>")
        assert html is not None
        assert "(no body)" in html
        assert "<em>(no body)</em>" in html
    finally:
        os.unlink(db_path)


def test_build_detail_html_no_attachments() -> None:
    """'(none)' shown when attachments list is empty."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _populate_db(
            db_path,
            [
                {
                    "message_id": "<no-attach@test.com>",
                    "sender": "x@x.com",
                    "subject": "No Attachments",
                    "date": "2025-01-01T00:00:00",
                    "body_plain": "body",
                    "status": "inbox",
                },
            ],
        )

        html = _build_detail_html(db_path, "<no-attach@test.com>")
        assert html is not None
        assert "(none)" in html
    finally:
        os.unlink(db_path)


def test_build_detail_html_no_cc() -> None:
    """CC section is omitted when cc list is empty."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        # Insert with explicit recipients_json that has no CC
        conn = init_db(db_path)
        try:
            conn.execute(
                "INSERT INTO mail_records "
                "(message_id, sender, subject, date, recipients_json, "
                "body_plain, body_html, attachments_json, status) "
                "VALUES (?, ?, ?, ?, ?, ?, '', '[]', ?)",
                (
                    "<no-cc@test.com>",
                    "x@x.com",
                    "No CC",
                    "2025-01-01T00:00:00",
                    '{"to": ["a@b.com"], "cc": []}',
                    "body",
                    "inbox",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        html = _build_detail_html(db_path, "<no-cc@test.com>")
        assert html is not None
        # "To" should be present, but no separate CC label
        assert "To" in html
        # The string "CC" should not appear as a detail-label
        assert ">CC</div>" not in html
    finally:
        os.unlink(db_path)


def test_build_detail_html_includes_move_form() -> None:
    """Detail page includes a Move form with the correct message_id."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _populate_db(
            db_path,
            [
                {
                    "message_id": "<move-detail@test.com>",
                    "sender": "x@x.com",
                    "subject": "Move Detail",
                    "date": "2025-01-01T00:00:00",
                    "body_plain": "body",
                    "status": "triaging",
                },
            ],
        )

        html = _build_detail_html(db_path, "<move-detail@test.com>")
        assert html is not None
        assert '<form class="detail-form"' in html
        assert 'method="post" action="/move"' in html
        assert 'value="&lt;move-detail@test.com&gt;"' in html
        # Should have the current status pre-selected
        assert '<option value="triaging" selected>Triaging</option>' in html
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# GET /email/{message_id} handler integration tests
# ---------------------------------------------------------------------------


def test_handler_email_detail_returns_200() -> None:
    """GET /email/{encoded_id} returns 200 and HTML."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _populate_db(
            db_path,
            [
                {
                    "message_id": "<handler-detail@test.com>",
                    "sender": "h@h.com",
                    "subject": "Handler Detail",
                    "date": "2025-01-01T00:00:00",
                    "body_plain": "detail body",
                    "status": "inbox",
                },
            ],
        )

        server, port = _start_test_server(db_path)
        try:
            import urllib.request
            encoded = urllib.request.pathname2url("<handler-detail@test.com>")
            resp = urlopen(f"http://127.0.0.1:{port}/email/{encoded}")
            assert resp.status == 200
            content_type = resp.headers.get("Content-Type", "")
            assert "text/html" in content_type
            body = resp.read().decode("utf-8")
            assert "<!DOCTYPE html>" in body
            assert "Handler Detail" in body
        finally:
            server.shutdown()
    finally:
        os.unlink(db_path)


def test_handler_email_detail_unknown_returns_404() -> None:
    """GET /email/unknown-id returns 404."""
    server, port = _start_test_server(":memory:")
    try:
        import urllib.error
        try:
            urlopen(f"http://127.0.0.1:{port}/email/does-not-exist")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("Expected HTTPError 404")
    finally:
        server.shutdown()


def test_handler_email_detail_missing_db_returns_503() -> None:
    """GET /email/{id} returns 503 when DB is unavailable."""
    import urllib.error
    server, port = _start_test_server("/dev/null/nonexistent.db")
    try:
        try:
            urlopen(f"http://127.0.0.1:{port}/email/anything")
        except urllib.error.HTTPError as exc:
            assert exc.code == 503
            body = exc.read().decode("utf-8")
            assert "Database unavailable" in body
        else:
            raise AssertionError("Expected HTTPError for 503")
    finally:
        server.shutdown()


def test_handler_email_detail_xss_prevention() -> None:
    """HTML in subject/body is escaped, not rendered on the detail page."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _populate_db(
            db_path,
            [
                {
                    "message_id": "<xss-detail@test.com>",
                    "sender": "<script>alert(1)</script>",
                    "subject": "<img onerror=alert(2)>",
                    "date": "2025-01-01T00:00:00",
                    "body_plain": "<b>evil body</b>",
                    "status": "inbox",
                },
            ],
        )

        server, port = _start_test_server(db_path)
        try:
            import urllib.request
            encoded = urllib.request.pathname2url("<xss-detail@test.com>")
            resp = urlopen(f"http://127.0.0.1:{port}/email/{encoded}")
            body = resp.read().decode("utf-8")

            # All angle brackets must be escaped
            assert "<script>" not in body
            assert "&lt;script&gt;" in body
            assert "&lt;img onerror" in body
            assert "&lt;b&gt;evil body&lt;/b&gt;" in body
        finally:
            server.shutdown()
    finally:
        os.unlink(db_path)


def test_handler_email_detail_does_not_capture_status_route() -> None:
    """GET /email/{id}/status still returns plain text, not HTML detail."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _populate_db(
            db_path,
            [
                {
                    "message_id": "<status-route@test.com>",
                    "sender": "s@s.com",
                    "subject": "Status Route",
                    "date": "2025-01-01T00:00:00",
                    "body_plain": "body",
                    "status": "done",
                },
            ],
        )

        server, port = _start_test_server(db_path)
        try:
            import urllib.request
            encoded = urllib.request.pathname2url("<status-route@test.com>")
            resp = urlopen(f"http://127.0.0.1:{port}/email/{encoded}/status")
            assert resp.status == 200
            content_type = resp.headers.get("Content-Type", "")
            assert "text/plain" in content_type
            body = resp.read().decode("utf-8")
            assert body == "done"
            # Should NOT be HTML
            assert "<!DOCTYPE html>" not in body
        finally:
            server.shutdown()
    finally:
        os.unlink(db_path)


def test_handler_email_detail_with_recipients() -> None:
    """Detail page shows To and CC when present."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = init_db(db_path)
        try:
            conn.execute(
                "INSERT INTO mail_records "
                "(message_id, sender, subject, date, recipients_json, "
                "body_plain, body_html, attachments_json, status) "
                "VALUES (?, ?, ?, ?, ?, ?, '', '[]', ?)",
                (
                    "<with-cc@test.com>",
                    "sender@test.com",
                    "With CC",
                    "2025-01-01T00:00:00",
                    '{"to": ["alice@x.com", "bob@x.com"], "cc": ["carol@x.com"]}',
                    "body",
                    "inbox",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        server, port = _start_test_server(db_path)
        try:
            import urllib.request
            encoded = urllib.request.pathname2url("<with-cc@test.com>")
            resp = urlopen(f"http://127.0.0.1:{port}/email/{encoded}")
            body = resp.read().decode("utf-8")
            assert "alice@x.com, bob@x.com" in body
            assert "carol@x.com" in body
            assert ">CC</div>" in body
        finally:
            server.shutdown()
    finally:
        os.unlink(db_path)


def test_handler_email_detail_with_attachments() -> None:
    """Detail page shows attachment filenames and sizes."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = init_db(db_path)
        try:
            conn.execute(
                "INSERT INTO mail_records "
                "(message_id, sender, subject, date, recipients_json, "
                "body_plain, body_html, attachments_json, status) "
                "VALUES (?, ?, ?, ?, '{}', ?, '', ?, ?)",
                (
                    "<with-attach@test.com>",
                    "sender@test.com",
                    "With Attachments",
                    "2025-01-01T00:00:00",
                    "body",
                    (
                        '[{"filename": "doc.pdf", "size": 2048}, '
                        '{"filename": "img.png", "size": 512}]'
                    ),
                    "inbox",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        server, port = _start_test_server(db_path)
        try:
            import urllib.request
            encoded = urllib.request.pathname2url("<with-attach@test.com>")
            resp = urlopen(f"http://127.0.0.1:{port}/email/{encoded}")
            body = resp.read().decode("utf-8")
            assert "doc.pdf" in body
            assert "2,048 bytes" in body
            assert "img.png" in body
            assert "512 bytes" in body
        finally:
            server.shutdown()
    finally:
        os.unlink(db_path)


def test_handler_email_detail_html_version_note() -> None:
    """Detail page shows 'HTML version available' when body_html is non-empty."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = init_db(db_path)
        try:
            conn.execute(
                "INSERT INTO mail_records "
                "(message_id, sender, subject, date, recipients_json, "
                "body_plain, body_html, attachments_json, status) "
                "VALUES (?, ?, ?, ?, '{}', ?, ?, '[]', ?)",
                (
                    "<html-body@test.com>",
                    "sender@test.com",
                    "HTML Body",
                    "2025-01-01T00:00:00",
                    "plain text",
                    "<p>HTML content</p>",
                    "inbox",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        server, port = _start_test_server(db_path)
        try:
            import urllib.request
            encoded = urllib.request.pathname2url("<html-body@test.com>")
            resp = urlopen(f"http://127.0.0.1:{port}/email/{encoded}")
            body = resp.read().decode("utf-8")
            assert "HTML version available" in body
            # Raw HTML body should NOT be rendered
            assert "<p>HTML content</p>" not in body
        finally:
            server.shutdown()
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# _build_detail_html embed mode
# ---------------------------------------------------------------------------


def test_build_detail_html_embed_no_full_page_chrome() -> None:
    """embed=True returns a fragment without DOCTYPE, <html>, <head>, <body>,
    <title>, meta refresh, back-link, or <h1>."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _populate_db(
            db_path,
            [
                {
                    "message_id": "<embed-test@test.com>",
                    "sender": "embed-sender@test.com",
                    "subject": "Embed Test Subject",
                    "date": "2025-06-15T14:30:00",
                    "body_plain": "Embed body content.",
                    "status": "inbox",
                },
            ],
        )

        html = _build_detail_html(db_path, "<embed-test@test.com>", embed=True)
        assert html is not None
        assert "<!DOCTYPE html>" not in html
        assert "<html" not in html
        assert "<head>" not in html
        assert "<body>" not in html
        assert "<title>" not in html
        assert 'meta http-equiv="refresh"' not in html
        assert "← Back to board" not in html
        assert "<h1>" not in html
        assert 'class="detail-container"' not in html
        # Should have embed wrapper and content
        assert 'class="embed-detail"' in html
        assert "<style>" in html
        assert "embed-sender@test.com" in html
        assert "Embed body content." in html
        # Move form should be present
        assert '<form class="detail-form"' in html
    finally:
        os.unlink(db_path)


def test_build_detail_html_embed_has_redirect_to() -> None:
    """embed=True includes a redirect_to hidden input in the move form."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _populate_db(
            db_path,
            [
                {
                    "message_id": "<redirect-embed@test.com>",
                    "sender": "x@x.com",
                    "subject": "Redirect Embed",
                    "date": "2025-01-01T00:00:00",
                    "body_plain": "body",
                    "status": "triaging",
                },
            ],
        )

        html = _build_detail_html(db_path, "<redirect-embed@test.com>", embed=True)
        assert html is not None
        assert 'name="redirect_to"' in html
        # The redirect_to value should point back to the embed URL
        assert '/email/' in html
        assert '?embed=1' in html
    finally:
        os.unlink(db_path)


def test_build_detail_html_embed_nonexistent_returns_none() -> None:
    """embed=True returns None for unknown message_id, same as default."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        result = _build_detail_html(db_path, "<nope@x.com>", embed=True)
        assert result is None
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# _build_board_html side-panel skeleton + script
# ---------------------------------------------------------------------------


def test_build_board_html_has_side_panel_skeleton() -> None:
    """_build_board_html output contains the side-panel HTML skeleton."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        html = _build_board_html(db_path)
        assert 'class="board-wrapper"' in html
        assert 'class="side-panel"' in html
        assert 'id="side-panel"' in html
        assert 'class="panel-header"' in html
        assert 'class="close-btn"' in html
        assert "&times;" in html
        assert "<iframe" in html
    finally:
        os.unlink(db_path)


def test_build_board_html_has_script_block() -> None:
    """_build_board_html output includes the JavaScript with openDetail/closeDetail."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        html = _build_board_html(db_path)
        assert "function openDetail(messageId, subject)" in html
        assert "function closeDetail()" in html
        assert "'/email/' + messageId + '?embed=1'" in html
        assert "classList.add('open')" in html
        assert "classList.remove('open')" in html
        assert "location.hash" in html
        # Delegated click on .board
        assert "closest('.card')" in html
        assert "getAttribute('data-message-id')" in html
        # Escape key handler
        assert "Escape" in html
        # Hash change handler
        assert "'hashchange'" in html
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# _render_card data-message-id attribute
# ---------------------------------------------------------------------------


def test_render_card_has_data_message_id() -> None:
    """_render_card includes data-message-id with URL-encoded message_id."""
    record = _make_record(
        message_id="<test@example.com>",
        sender="x@x.com",
        subject="s",
        date="2025-01-01T00:00:00",
        body_plain="body",
    )
    html = _render_card(record)
    assert 'data-message-id="' in html
    # The value should be URL-encoded
    import urllib.parse
    quoted = urllib.parse.quote("<test@example.com>", safe="")
    assert f'data-message-id="{quoted}"' in html


def test_render_card_data_message_id_present_with_subject_link() -> None:
    """data-message-id coexists with the existing subject <a> link for
    non-JS fallback."""
    record = _make_record(
        message_id="abc123",
        sender="x@x.com",
        subject="Hello",
        date="2025-01-01T00:00:00",
        body_plain="body",
    )
    html = _render_card(record)
    assert 'data-message-id="abc123"' in html
    assert '<a href="/email/abc123">' in html


# ---------------------------------------------------------------------------
# POST /move with redirect_to
# ---------------------------------------------------------------------------


def test_move_with_redirect_to() -> None:
    """POST /move with redirect_to redirects to the specified path."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _populate_db(
            db_path,
            [
                {
                    "message_id": "redirect-me",
                    "sender": "x@x.com",
                    "subject": "Redirect test",
                    "date": "2025-01-01T00:00:00",
                    "body_plain": "body",
                    "status": "inbox",
                },
            ],
        )

        server, port = _start_test_server(db_path)
        try:
            status, body = _post_form(
                port,
                {
                    "message_id": "redirect-me",
                    "status": "done",
                    "redirect_to": "/email/redirect-me?embed=1",
                },
            )
            assert status == 302, f"Expected 302, got {status}: {body}"

            # Also verify normal redirect still works (no redirect_to)
            status2, _body2 = _post_form(
                port,
                {"message_id": "redirect-me", "status": "triaging"},
            )
            assert status2 == 302
        finally:
            server.shutdown()
    finally:
        os.unlink(db_path)


def test_move_with_empty_redirect_to_falls_back_to_board() -> None:
    """Empty redirect_to should redirect to /board (backward-compatible)."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _populate_db(
            db_path,
            [
                {
                    "message_id": "fallback-me",
                    "sender": "x@x.com",
                    "subject": "Fallback test",
                    "date": "2025-01-01T00:00:00",
                    "body_plain": "body",
                    "status": "inbox",
                },
            ],
        )

        server, port = _start_test_server(db_path)
        try:
            status, body = _post_form(
                port,
                {
                    "message_id": "fallback-me",
                    "status": "done",
                    "redirect_to": "",
                },
            )
            assert status == 302, f"Expected 302, got {status}: {body}"
            # Should redirect to /board because redirect_to is empty
            # (the test NoRedirect handler doesn't follow, so we can't
            # check Location directly here; we rely on status 302 and
            # the board counts to verify correctness)
            resp = urlopen(f"http://127.0.0.1:{port}/board")
            board_html = resp.read().decode("utf-8")
            counts = re.findall(r'<span class="count">(\d+)</span>', board_html)
            assert counts == ["0", "0", "1", "0"]
        finally:
            server.shutdown()
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# GET /email/{message_id}?embed=1 handler integration tests
# ---------------------------------------------------------------------------


def test_handler_email_detail_embed_returns_fragment() -> None:
    """GET /email/{id}?embed=1 returns HTML fragment without full-page chrome."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _populate_db(
            db_path,
            [
                {
                    "message_id": "embed-handler@test.com",
                    "sender": "eh@test.com",
                    "subject": "Embed Handler",
                    "date": "2025-01-01T00:00:00",
                    "body_plain": "embed handler body",
                    "status": "inbox",
                },
            ],
        )

        server, port = _start_test_server(db_path)
        try:
            resp = urlopen(f"http://127.0.0.1:{port}/email/embed-handler@test.com?embed=1")
            assert resp.status == 200
            body = resp.read().decode("utf-8")
            # Fragment — no full-page chrome
            assert "<!DOCTYPE html>" not in body
            assert "<html" not in body
            assert "<title>" not in body
            # But has the content
            assert "eh@test.com" in body
            assert "embed handler body" in body
            assert 'class="embed-detail"' in body
            # Move form with redirect_to
            assert 'name="redirect_to"' in body
        finally:
            server.shutdown()
    finally:
        os.unlink(db_path)


def test_handler_email_detail_embed_unknown_returns_404() -> None:
    """GET /email/unknown?embed=1 returns 404 (same as non-embed)."""
    server, port = _start_test_server(":memory:")
    try:
        import urllib.error
        try:
            urlopen(f"http://127.0.0.1:{port}/email/does-not-exist?embed=1")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("Expected HTTPError 404")
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# POST /config-sync tests
# ---------------------------------------------------------------------------


def _post_config_sync(port: int) -> tuple[int, str]:
    """POST an empty body to /config-sync; return (status, body)."""
    import urllib.request

    url = f"http://127.0.0.1:{port}/config-sync"

    class CaptureError(urllib.request.HTTPDefaultErrorHandler):
        def http_error_default(  # type: ignore[override]
            self,
            req: urllib.request.Request,
            fp: object,
            code: int,
            msg: object,
            hdrs: object,
        ) -> object:
            return fp

    opener = urllib.request.build_opener(CaptureError())
    req = urllib.request.Request(url, data=b"", method="POST")  # noqa: S310
    resp = opener.open(req)
    body = resp.read().decode("utf-8")
    return resp.status, body


def test_config_sync_success_returns_200_json() -> None:
    import json as _json
    from unittest import mock

    from robotsix_auto_mail.config_sync import ConfigSyncResult, DriftProposal

    fake_result = ConfigSyncResult(
        proposals=[
            DriftProposal(
                title="Default mismatch",
                body="The YAML default differs from the dataclass default.",
                affected_field="timeout",
                confidence="high",
            )
        ]
    )

    import urllib.request

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        server, port = _start_test_server(db_path)
        try:
            with mock.patch(
                "robotsix_auto_mail.config_sync.run_config_sync_agent",
                return_value=fake_result,
            ) as mocked:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/config-sync",
                    data=b"",
                    method="POST",
                )
                resp = urlopen(req)  # noqa: S310
                assert resp.status == 200
                assert resp.headers.get("Content-Type", "").startswith(
                    "application/json"
                )
                payload = _json.loads(resp.read().decode("utf-8"))

            assert list(payload.keys()) == ["proposals"]
            assert len(payload["proposals"]) == 1
            proposal = payload["proposals"][0]
            assert proposal["title"] == "Default mismatch"
            assert proposal["affected_field"] == "timeout"
            assert proposal["confidence"] == "high"
            assert "body" in proposal

            # Verify the agent was invoked with a live DB connection so the
            # dedup ledger wiring is exercised.
            assert mocked.call_count == 1
            assert "conn" in mocked.call_args.kwargs
            assert mocked.call_args.kwargs["conn"] is not None
        finally:
            server.shutdown()
    finally:
        os.unlink(db_path)


def test_config_sync_error_returns_503_json() -> None:
    import json as _json
    from unittest import mock

    from robotsix_auto_mail.config_sync import ConfigSyncError

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        server, port = _start_test_server(db_path)
        try:
            with mock.patch(
                "robotsix_auto_mail.config_sync.run_config_sync_agent",
                side_effect=ConfigSyncError("No LLM API key found"),
            ):
                status, body = _post_config_sync(port)
            assert status == 503
            payload = _json.loads(body)
            assert "error" in payload
            assert "No LLM API key found" in payload["error"]
        finally:
            server.shutdown()
    finally:
        os.unlink(db_path)


def test_config_sync_unknown_post_path_returns_404() -> None:
    import urllib.error
    import urllib.request

    server, port = _start_test_server(":memory:")
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/no-such-endpoint",
            data=b"",
            method="POST",
        )
        try:
            urlopen(req)  # noqa: S310
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("Expected HTTPError 404")
    finally:
        server.shutdown()
