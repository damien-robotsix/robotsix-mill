"""Tests for the server module (kanban board HTTP handler)."""

from __future__ import annotations

import os
import re
import tempfile
from typing import TYPE_CHECKING
from urllib.request import urlopen

if TYPE_CHECKING:
    from http.server import HTTPServer

from robotsix_auto_mail.db import MailRecord, init_db
from robotsix_auto_mail.format import _format_date
from robotsix_auto_mail.server import _build_board_html, _render_card

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
    record = MailRecord(
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
    record = MailRecord(
        message_id="abc",
        sender="x",
        subject="   ",
        date="2025-01-01T00:00:00",
        body_plain="body",
    )
    html = _render_card(record)
    assert "(no subject)" in html


def test_render_card_empty_body_plain() -> None:
    record = MailRecord(
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
    record = MailRecord(
        message_id="abc",
        sender="x",
        subject="s",
        date="2025-01-01T00:00:00",
        body_plain="   \t\n  ",
    )
    html = _render_card(record)
    assert "(no body)" in html


def test_render_card_body_truncation() -> None:
    record = MailRecord(
        message_id="abc",
        sender="x",
        subject="s",
        date="2025-01-01T00:00:00",
        body_plain="A" * 200,
    )
    html = _render_card(record)
    # Should contain exactly 100 chars of "A" then "…"
    assert ("A" * 100 + "\u2026") in html


def test_render_card_body_exactly_limit() -> None:
    body = "B" * 100
    record = MailRecord(
        message_id="abc",
        sender="x",
        subject="s",
        date="2025-01-01T00:00:00",
        body_plain=body,
    )
    html = _render_card(record)
    assert body in html
    # No ellipsis for exact 100
    assert "\u2026" not in html


def test_render_card_html_escapes_sender() -> None:
    record = MailRecord(
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
    record = MailRecord(
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
    record = MailRecord(
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
    record = MailRecord(
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
    record = MailRecord(
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
        long_body = "X" * 150
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
        # The body preview should be exactly 100 chars + "…"
        assert ("X" * 100 + "\u2026") in html
        assert ("X" * 101) not in html
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

            # All angle brackets must be escaped
            assert "<script>" not in body
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
    req = urllib.request.Request(
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


def test_email_path_without_status_suffix_returns_404() -> None:
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
            import urllib.error

            try:
                urlopen(f"http://127.0.0.1:{port}/email/mid1")
            except urllib.error.HTTPError as exc:
                assert exc.code == 404
            else:
                raise AssertionError("Expected HTTPError 404")
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