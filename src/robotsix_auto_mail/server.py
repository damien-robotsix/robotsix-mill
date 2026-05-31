"""HTTP server for the read-only kanban mail board.

Provides ``make_board_handler`` — a factory that returns a
``BaseHTTPRequestHandler`` subclass wired to a specific SQLite database
path.
"""

from __future__ import annotations

import html
import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, quote, unquote

from robotsix_auto_mail.db import MailRecord
from robotsix_auto_mail.format import _format_date
from robotsix_auto_mail.status import STATUS_ORDER, VALID_STATUSES

_BOARD_COLUMNS = STATUS_ORDER

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
.card-form button { font-size: 0.75rem; padding: 0.1rem 0.5rem; cursor: pointer; }
.card .subject a { color: inherit; text-decoration: none; }
.card .subject a:hover { text-decoration: underline; }

/* Detail page */
.back-link {
    display: inline-block; margin-bottom: 1rem;
    color: #333; text-decoration: none;
}
.back-link:hover { text-decoration: underline; }
.detail-container { max-width: 800px; }
.detail-field { margin-bottom: 0.75rem; }
.detail-label {
    font-weight: 700; font-size: 0.85rem;
    color: #666; margin-bottom: 0.15rem;
}
.detail-value { font-size: 0.95rem; }
.detail-value pre { margin: 0; white-space: pre-wrap; font-family: inherit; }
.detail-value code {
    font-size: 0.85rem; background: #eee;
    padding: 0.1rem 0.3rem; border-radius: 3px;
}
.detail-form { margin-top: 0.25rem; display: flex; gap: 0.25rem; align-items: center; }
.detail-form select { font-size: 0.8rem; padding: 0.15rem 0.3rem; }
.detail-form button { font-size: 0.8rem; padding: 0.15rem 0.6rem; cursor: pointer; }

/* Side panel */
.board-wrapper {
  transition: margin-right 0.3s ease;
}
.board-wrapper.panel-open {
  margin-right: 45vw;
}
.side-panel {
  position: fixed;
  top: 0; right: 0;
  width: 45vw; max-width: 100vw; height: 100vh;
  background: #fff;
  box-shadow: -2px 0 8px rgba(0,0,0,0.15);
  border-left: 1px solid #ddd;
  z-index: 1000;
  display: flex; flex-direction: column;
  transform: translateX(100%);
  transition: transform 0.3s ease;
  overflow-y: auto;
}
.side-panel.open {
  transform: translateX(0);
}
.panel-header {
  display: flex; justify-content: space-between;
  align-items: center;
  padding: 0.75rem 1rem;
  border-bottom: 1px solid #ddd;
  background: #f5f5f5;
}
.panel-header .panel-title {
  font-weight: 600; font-size: 0.95rem;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.panel-header .close-btn {
  background: none; border: none;
  font-size: 1.25rem; cursor: pointer;
  padding: 0 0.25rem; line-height: 1; color: #666;
}
.panel-header .close-btn:hover { color: #000; }
.side-panel iframe {
  flex: 1; border: none; width: 100%;
}"""


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
        '<div class="board-wrapper">\n'
        '<div class="board">\n'
        + "".join(columns_html_parts)
        + "\n</div>\n"
        "</div>\n"
        '<div class="side-panel" id="side-panel">\n'
        '<div class="panel-header">\n'
        '<span class="panel-title"></span>\n'
        '<button class="close-btn" onclick="closeDetail()">&times;</button>\n'
        "</div>\n"
        '<iframe src="" title="Mail detail"></iframe>\n'
        "</div>\n"
        "<script>\n"
        "function openDetail(messageId, subject) {\n"
        "  document.querySelector('.side-panel iframe').src"
        " = '/email/' + messageId + '?embed=1';\n"
        "  document.querySelector('.side-panel').classList.add('open');\n"
        "  document.querySelector('.board-wrapper').classList.add('panel-open');\n"
        "  document.querySelector('.panel-title').textContent = subject || '';\n"
        "  location.hash = messageId;\n"
        "}\n"
        "function closeDetail() {\n"
        "  document.querySelector('.side-panel').classList.remove('open');\n"
        "  document.querySelector('.board-wrapper').classList.remove('panel-open');\n"
        "  document.querySelector('.side-panel iframe').src = '';\n"
        "  location.hash = '';\n"
        "}\n"
        "if (location.hash) {\n"
        "  var mid = location.hash.slice(1);\n"
        "  if (mid) openDetail(mid);\n"
        "}\n"
        "window.addEventListener('hashchange', function() {\n"
        "  if (!location.hash) closeDetail();\n"
        "});\n"
        "window.addEventListener('keydown', function(e) {\n"
        "  if (e.key === 'Escape') closeDetail();\n"
        "});\n"
        "document.querySelector('.board').addEventListener('click', function(e) {\n"
        "  var card = e.target.closest('.card');\n"
        "  if (!card) return;\n"
        "  var mid = card.getAttribute('data-message-id');\n"
        "  if (!mid) return;\n"
        "  e.preventDefault();\n"
        "  var subject = card.getAttribute('data-subject') || '';\n"
        "  openDetail(mid, subject);\n"
        "});\n"
        "</script>\n"
        "</body>\n"
        "</html>"
    )


def _build_detail_html(
    db_path: str, message_id: str, *, embed: bool = False,
) -> str | None:
    """Build a full HTML detail page for a single ``MailRecord``.

    Returns the HTML string, or ``None`` when *message_id* is not found.
    Raises an exception on database errors (caller catches for 503).
    """
    from robotsix_auto_mail.db import get_record_by_message_id, init_db

    conn = init_db(db_path)
    try:
        record = get_record_by_message_id(conn, message_id)
    finally:
        conn.close()

    if record is None:
        return None

    # Parse JSON fields
    try:
        recipients = json.loads(record.recipients_json)
    except (json.JSONDecodeError, TypeError):
        recipients = {"to": [], "cc": []}
    to_list: list[str] = (
        recipients.get("to", []) if isinstance(recipients, dict) else []
    )
    cc_list: list[str] = (
        recipients.get("cc", []) if isinstance(recipients, dict) else []
    )

    try:
        attachments = json.loads(record.attachments_json)
    except (json.JSONDecodeError, TypeError):
        attachments = []
    if not isinstance(attachments, list):
        attachments = []

    # Status options
    options_parts: list[str] = []
    for s in STATUS_ORDER:
        sel = ' selected' if s == record.status else ''
        options_parts.append(
            f'<option value="{html.escape(s)}"{sel}>'
            f'{html.escape(s.capitalize())}</option>'
        )

    quoted_mid = quote(record.message_id, safe="")
    redirect_input = ""
    if embed:
        redirect_input = (
            '<input type="hidden" name="redirect_to"'
            f' value="/email/{html.escape(quoted_mid)}?embed=1">'
        )
    move_form = (
        '<form class="detail-form" method="post" action="/move">'
        f'<input type="hidden" name="message_id"'
        f' value="{html.escape(record.message_id)}">'
        f'{redirect_input}'
        f'<select name="status">{"".join(options_parts)}</select>'
        '<button type="submit">Move</button>'
        '</form>'
    )

    # Subject for title (truncated to ~60 chars)
    raw_subject = record.subject.strip() or "(no subject)"
    title_subject = raw_subject[:60] + ("…" if len(raw_subject) > 60 else "")

    # Date
    date_str = html.escape(_format_date(record.date))

    # Body plain
    body = record.body_plain
    if not body or not body.strip():
        body_html = '<span class="detail-value"><em>(no body)</em></span>'
    else:
        body_html = f'<pre>{html.escape(body)}</pre>'

    # Body HTML note
    body_html_note = ""
    if record.body_html.strip():
        body_html_note = (
            '<div class="detail-field">'
            '<div class="detail-label">HTML version</div>'
            '<div class="detail-value"><em>HTML version available</em></div>'
            '</div>'
        )

    # Recipients
    to_html = html.escape(", ".join(to_list)) if to_list else "<em>(none)</em>"
    cc_section = ""
    if cc_list:
        cc_html = html.escape(", ".join(cc_list))
        cc_section = (
            '<div class="detail-field">'
            '<div class="detail-label">CC</div>'
            f'<div class="detail-value">{cc_html}</div>'
            '</div>'
        )

    # Attachments
    if attachments and isinstance(attachments, list) and len(attachments) > 0:
        attach_parts: list[str] = []
        for a in attachments:
            if isinstance(a, dict):
                fname = html.escape(str(a.get("filename", "?")))
                fsize = a.get("size")
                if fsize is not None and isinstance(fsize, (int, float)):
                    fsize_str = f" ({int(fsize):,} bytes)"
                else:
                    fsize_str = ""
                attach_parts.append(f"{fname}{fsize_str}")
            else:
                attach_parts.append(html.escape(str(a)))
        attach_html = ", ".join(attach_parts)
    else:
        attach_html = "<em>(none)</em>"

    # IMAP UID
    imap_uid_section = ""
    if record.imap_uid is not None:
        imap_uid_section = (
            '<div class="detail-field">'
            '<div class="detail-label">IMAP UID</div>'
            f'<div class="detail-value"><code>{record.imap_uid}</code></div>'
            '</div>'
        )

    if embed:
        return (
            '<style>\n'
            '.detail-field { margin-bottom: 0.75rem; }\n'
            '.detail-label { font-weight: 700; font-size: 0.85rem; color: #666;'
            ' margin-bottom: 0.15rem; }\n'
            '.detail-value { font-size: 0.95rem; }\n'
            '.detail-value pre { margin: 0; white-space: pre-wrap;'
            ' font-family: inherit; }\n'
            '.detail-value code { font-size: 0.85rem; background: #eee;'
            ' padding: 0.1rem 0.3rem; border-radius: 3px; }\n'
            '.detail-form { margin-top: 0.25rem; display: flex; gap: 0.25rem;'
            ' align-items: center; }\n'
            '.detail-form select { font-size: 0.8rem; padding: 0.15rem 0.3rem; }\n'
            '.detail-form button { font-size: 0.8rem; padding: 0.15rem 0.6rem;'
            ' cursor: pointer; }\n'
            '.embed-detail { padding: 1rem;'
            ' font-family: system-ui, -apple-system, sans-serif; }\n'
            '</style>\n'
            '<div class="embed-detail">\n'
            '<div class="detail-field">'
            '<div class="detail-label">Sender</div>'
            f'<div class="detail-value"><strong>{html.escape(record.sender)}'
            '</strong></div>'
            '</div>\n'
            '<div class="detail-field">'
            '<div class="detail-label">Date</div>'
            f'<div class="detail-value">{date_str}</div>'
            '</div>\n'
            '<div class="detail-field">'
            '<div class="detail-label">Status</div>'
            f'<div class="detail-value">{html.escape(record.status.capitalize())}'
            f'{move_form}</div>'
            '</div>\n'
            '<div class="detail-field">'
            '<div class="detail-label">To</div>'
            f'<div class="detail-value">{to_html}</div>'
            '</div>\n'
            f'{cc_section}'
            '<div class="detail-field">'
            '<div class="detail-label">Body</div>'
            f'<div class="detail-value">{body_html}</div>'
            '</div>\n'
            f'{body_html_note}'
            '<div class="detail-field">'
            '<div class="detail-label">Attachments</div>'
            f'<div class="detail-value">{attach_html}</div>'
            '</div>\n'
            '<div class="detail-field">'
            '<div class="detail-label">Message ID</div>'
            f'<div class="detail-value"><code>{html.escape(record.message_id)}'
            '</code></div>'
            '</div>\n'
            f'{imap_uid_section}'
            '</div>\n'
        )

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f"<title>Mail: {html.escape(title_subject)}</title>\n"
        '<meta http-equiv="refresh" content="30">\n'
        f"<style>{_CSS}</style>\n"
        "</head>\n"
        "<body>\n"
        '<a class="back-link" href="/board">← Back to board</a>\n'
        '<div class="detail-container">\n'
        f'<h1>{html.escape(record.subject.strip() or "(no subject)")}</h1>\n'
        '<div class="detail-field">'
        '<div class="detail-label">Sender</div>'
        f'<div class="detail-value"><strong>{html.escape(record.sender)}</strong></div>'
        '</div>\n'
        '<div class="detail-field">'
        '<div class="detail-label">Date</div>'
        f'<div class="detail-value">{date_str}</div>'
        '</div>\n'
        '<div class="detail-field">'
        '<div class="detail-label">Status</div>'
        f'<div class="detail-value">{html.escape(record.status.capitalize())}'
        f'{move_form}</div>'
        '</div>\n'
        '<div class="detail-field">'
        '<div class="detail-label">To</div>'
        f'<div class="detail-value">{to_html}</div>'
        '</div>\n'
        f'{cc_section}'
        '<div class="detail-field">'
        '<div class="detail-label">Body</div>'
        f'<div class="detail-value">{body_html}</div>'
        '</div>\n'
        f'{body_html_note}'
        '<div class="detail-field">'
        '<div class="detail-label">Attachments</div>'
        f'<div class="detail-value">{attach_html}</div>'
        '</div>\n'
        '<div class="detail-field">'
        '<div class="detail-label">Message ID</div>'
        f'<div class="detail-value"><code>{html.escape(record.message_id)}</code></div>'
        '</div>\n'
        f'{imap_uid_section}'
        '</div>\n'
        "</body>\n"
        "</html>"
    )


def _render_card(record: MailRecord) -> str:
    """Render a single ``MailRecord`` as a ``.card`` HTML string."""
    sender = html.escape(record.sender)
    subject = html.escape(record.subject) if record.subject.strip() else "(no subject)"
    subject_attr = html.escape(record.subject.strip() or "(no subject)")
    quoted_mid = quote(record.message_id, safe="")
    subject_html = f'<a href="/email/{quoted_mid}">{subject}</a>'
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
        f'<div class="card" data-message-id="{quoted_mid}"'
        f' data-subject="{subject_attr}">'
        f'<div class="sender">{sender}</div>'
        f'<div class="subject">{subject_html}</div>'
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

        def do_GET(self) -> None:
            """Route GET requests."""
            if self.path == "/":
                self._redirect("/board")
            elif self.path == "/board":
                self._serve_board()
            elif self.path.startswith("/email/") and self.path.endswith("/status"):
                self._serve_email_status()
            elif self.path.startswith("/email/"):
                self._serve_email_detail()
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

        def do_POST(self) -> None:
            """Route POST requests."""
            if self.path == "/move":
                self._handle_move()
            else:
                self._not_found()

        def _handle_move(self) -> None:
            """Process POST /move — update a card's status and redirect."""
            from robotsix_auto_mail.db import init_db
            from robotsix_auto_mail.status import set_status

            content_length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_length).decode("utf-8")
            fields = parse_qs(raw)

            # parse_qs returns {key: [value, ...]} — extract first value.
            message_id = (fields.get("message_id") or [""])[0].strip()
            new_status = (fields.get("status") or [""])[0].strip()
            redirect_to = (fields.get("redirect_to") or [""])[0].strip()

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

            if redirect_to and redirect_to.startswith("/"):
                self._redirect(redirect_to, code=302)
            else:
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

        def _serve_email_detail(self) -> None:
            """Serve GET /email/{message_id} — full detail page.

            Supports ``?embed=1`` to return a fragment suitable for an
            iframe (no full-page chrome, no refresh).
            """
            from urllib.parse import parse_qs, urlparse

            path = self.path
            prefix = "/email/"

            # Separate path from query string.
            parsed = urlparse(path)
            message_id = unquote(parsed.path[len(prefix):])
            qs = parse_qs(parsed.query)
            embed = qs.get("embed", ["0"])[0] == "1"

            try:
                detail_html = _build_detail_html(
                    self.db_path, message_id, embed=embed,
                )
            except Exception:
                self.send_response(503)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Database unavailable")
                return

            if detail_html is None:
                self._not_found()
                return

            encoded = detail_html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            """Suppress logging to stderr (keep server quiet)."""
            pass

    return BoardHandler
