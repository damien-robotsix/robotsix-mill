"""HTTP server for the read-only kanban mail board.

Provides ``make_board_handler`` — a factory that returns a
``BaseHTTPRequestHandler`` subclass wired to a specific SQLite database
path.
"""

from __future__ import annotations

import html
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, unquote

from robotsix_auto_mail.db import MailRecord
from robotsix_auto_mail.status import STATUS_ORDER

_BOARD_COLUMNS = STATUS_ORDER


def _format_date(raw: str) -> str:
    """Parse an ISO-8601 *raw* date and return a human-friendly string.

    Returns *raw* unchanged when parsing fails.  This is a copy of the
    identically-named helper in ``cli.py`` so that the two modules stay
    independent.
    """
    try:
        dt = datetime.fromisoformat(raw)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return raw


_BODY_PREVIEW_LIMIT = 100

_CSS = """\
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: system-ui, -apple-system, sans-serif;
  background: #e8e8e8; padding: 1.5rem;
}
h1 { margin-bottom: 1rem; font-size: 1.5rem; }
.board { display: flex; gap: 1rem; overflow-x: auto; }
.column {
  flex: 1; min-width: 260px; background: #f5f5f5;
  border-radius: 8px; padding: 0.75rem;
}
.column-header {
  display: flex; justify-content: space-between;
  align-items: center; margin-bottom: 0.75rem;
  padding-bottom: 0.5rem; border-bottom: 2px solid #ddd;
}
.column-header h2 {
  font-size: 1rem; font-weight: 600; text-transform: capitalize;
}
.count {
  background: #666; color: #fff; font-size: 0.75rem;
  font-weight: 600; padding: 0.15rem 0.5rem; border-radius: 999px;
}
.cards { display: flex; flex-direction: column; gap: 0.5rem; }
.card {
  background: #fff; border: 1px solid #ddd;
  border-radius: 6px; padding: 0.6rem 0.75rem;
}
.card .sender {
  font-weight: 700; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap;
}
.card .subject { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.card .date { font-size: 0.8rem; color: #888; }
.card .body-preview { font-size: 0.85rem; color: #444; margin-top: 0.25rem; }
.card .no-body { font-style: italic; color: #999; }
.card-form { margin-top: 0.4rem; display: flex; gap: 0.25rem; align-items: center; }
.card-form select { font-size: 0.75rem; padding: 0.1rem 0.2rem; }
.card-form button { font-size: 0.75rem; padding: 0.1rem 0.5rem; cursor: pointer; }"""


def _build_board_html(db_path: str) -> str:
    """Build the full ``/board`` HTML document.

    Opens a fresh database connection, queries all four status columns,
    and returns a string.  Raises ``Exception`` when the database cannot
    be opened (the caller should catch it and return a 503).
    """
    from robotsix_auto_mail.db import init_db
    from robotsix_auto_mail.status import list_by_status

    conn = init_db(db_path)
    try:
        # Gather records per status in fixed column order.
        columns: list[tuple[str, list[MailRecord]]] = []
        for status in _BOARD_COLUMNS:
            records = list_by_status(conn, status)
            columns.append((status, records))
    finally:
        conn.close()

    # Build column HTML fragments.
    columns_html_parts: list[str] = []
    for status, records in columns:
        title = status.capitalize()
        count = len(records)
        cards_html = "".join(_render_card(r) for r in records)
        columns_html_parts.append(
            f'<div class="column">'
            f'<div class="column-header"><h2>{html.escape(title)}</h2>'
            f'<span class="count">{count}</span></div>'
            f'<div class="cards">{cards_html}</div>'
            f'</div>'
        )

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        "<title>Mail Board</title>\n"
        '<meta http-equiv="refresh" content="30">\n'
        f"<style>{_CSS}</style>\n"
        "</head>\n"
        "<body>\n"
        "<h1>Mail Board</h1>\n"
        '<div class="board">\n'
        + "".join(columns_html_parts)
        + "\n</div>\n"
        "</body>\n"
        "</html>"
    )


def _render_card(record: MailRecord) -> str:
    """Render a single ``MailRecord`` as a ``.card`` HTML string."""
    sender = html.escape(record.sender)
    subject = html.escape(record.subject) if record.subject.strip() else "(no subject)"
    date_str = html.escape(_format_date(record.date))

    body = record.body_plain
    if not body or not body.strip():
        body_html = '<span class="no-body">(no body)</span>'
    elif len(body) > _BODY_PREVIEW_LIMIT:
        body_html = html.escape(body[:_BODY_PREVIEW_LIMIT]) + "…"
    else:
        body_html = html.escape(body)

    # Build status dropdown with current status pre-selected.
    options_parts: list[str] = []
    for s in _BOARD_COLUMNS:
        sel = ' selected' if s == record.status else ''
        options_parts.append(
            f'<option value="{html.escape(s)}"{sel}>'
            f'{html.escape(s.capitalize())}</option>'
        )

    form_html = (
        '<form class="card-form" method="post" action="/move">'
        f'<input type="hidden" name="message_id"'
        f' value="{html.escape(record.message_id)}">'
        f'<select name="status">{"".join(options_parts)}</select>'
        '<button type="submit">Move</button>'
        '</form>'
    )

    return (
        f'<div class="card">'
        f'<div class="sender">{sender}</div>'
        f'<div class="subject">{subject}</div>'
        f'<div class="date">{date_str}</div>'
        f'<div class="body-preview">{body_html}</div>'
        f'{form_html}'
        f'</div>'
    )


def make_board_handler(
    db_path: str,
) -> type[BaseHTTPRequestHandler]:
    """Return a ``BaseHTTPRequestHandler`` subclass wired to *db_path*.

    The handler routes ``GET /`` to a 301 redirect to ``/board``, ``GET
    /board`` to the kanban board HTML page, and everything else to 404.
    """

    # Capture db_path for the handler class via a closure-friendly name.
    _db = db_path

    class BoardHandler(BaseHTTPRequestHandler):
        """Request handler for the robotsix-auto-mail board server."""

        # Class attribute so every request can find the database.
        db_path: str = _db

        def do_GET(self) -> None:  # noqa: N802
            """Route GET requests."""
            if self.path == "/":
                self._redirect("/board")
            elif self.path == "/board":
                self._serve_board()
            elif self.path.startswith("/email/") and self.path.endswith("/status"):
                self._serve_email_status()
            else:
                self._not_found()

        def _redirect(self, location: str, code: int = 301) -> None:
            """Send a redirect to *location*."""
            self.send_response(code)
            self.send_header("Location", location)
            self.end_headers()

        def _serve_board(self) -> None:
            """Render and serve the kanban board HTML."""
            try:
                body = _build_board_html(self.db_path)
            except Exception:
                self.send_response(503)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Database unavailable")
                return

            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _not_found(self) -> None:
            """Send a 404 Not Found."""
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Not found")

        def _bad_request(self, message: str) -> None:
            """Send a 400 Bad Request with a plain-text body."""
            encoded = message.encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self) -> None:  # noqa: N802
            """Route POST requests."""
            if self.path == "/move":
                self._handle_move()
            else:
                self._not_found()

        def _handle_move(self) -> None:
            """Process POST /move — update a card's status and redirect."""
            from robotsix_auto_mail.db import init_db
            from robotsix_auto_mail.status import VALID_STATUSES, set_status

            content_length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_length).decode("utf-8")
            fields = parse_qs(raw)

            # parse_qs returns {key: [value, ...]} — extract first value.
            message_id = (fields.get("message_id") or [""])[0].strip()
            new_status = (fields.get("status") or [""])[0].strip()

            if not message_id or not new_status:
                self._bad_request("Missing message_id or status")
                return

            if new_status not in VALID_STATUSES:
                self._bad_request(f"Invalid status: {new_status!r}")
                return

            conn = init_db(self.db_path)
            try:
                ok = set_status(conn, message_id, new_status)
            finally:
                conn.close()

            if not ok:
                self._not_found()
                return

            self._redirect("/board", code=302)

        def _serve_email_status(self) -> None:
            """Serve GET /email/{message_id}/status — return status as text."""
            from robotsix_auto_mail.db import init_db
            from robotsix_auto_mail.status import get_status

            # Extract the URL-encoded message_id from the path:
            #   "/email/<encoded>/status" → extract and decode.
            path = self.path
            prefix = "/email/"
            suffix = "/status"
            encoded_mid = path[len(prefix) : -len(suffix)]
            message_id = unquote(encoded_mid)

            conn = init_db(self.db_path)
            try:
                status = get_status(conn, message_id)
            finally:
                conn.close()

            if status is None:
                self._not_found()
                return

            encoded_body = status.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded_body)))
            self.end_headers()
            self.wfile.write(encoded_body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            """Suppress logging to stderr (keep server quiet)."""
            pass

    return BoardHandler