"""MIME parser: raw bytes → structured ``MailRecord``.

Uses the stdlib :mod:`email` module with ``policy.default`` to parse
raw MIME bytes into a fully-populated ``MailRecord`` ready for storage.
Handles single-part messages, ``multipart/alternative``,
``multipart/mixed``, nested multipart trees, RFC 2047 encoded headers,
and RFC 2231 attachment filenames.  Malformed or missing headers never
cause a crash; unparseable input raises ``ParseError``.
"""

from __future__ import annotations

import email.message
import email.policy
import email.utils
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_auto_mail.db import MailRecord


class ParseError(Exception):
    """Raised when the input bytes are not valid MIME at all."""


# ---------------------------------------------------------------------------
# Charset fallback chain
# ---------------------------------------------------------------------------

_CHARSET_FALLBACK = ("utf-8", "latin-1", "ascii")


def _decode_content(part: email.message.EmailMessage) -> str:
    """Decode a MIME part's payload, trying declared charset then fallbacks."""
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    if not isinstance(payload, bytes):
        # multipart or already-decoded str — shouldn't happen for leaves
        return str(payload)

    charset = part.get_content_charset()
    charsets = [charset] if charset else []
    charsets.extend(_CHARSET_FALLBACK)

    for cs in charsets:
        try:
            return payload.decode(cs, errors="replace")
        except (LookupError, ValueError):
            continue

    return payload.decode("ascii", errors="replace")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_message(
    raw_bytes: bytes,
    *,
    imap_uid: int | None = None,
) -> MailRecord:
    """Parse raw MIME bytes into a ``MailRecord``.

    Parameters
    ----------
    raw_bytes:
        The raw MIME message bytes (as returned by IMAP fetch).
    imap_uid:
        Optional IMAP UID supplied by the caller (e.g. from
        ``fetch_new_messages``).  Passed through to
        ``MailRecord.imap_uid``.

    Returns
    -------
    MailRecord
        A fully-populated record ready for ``insert_record``.

    Raises
    ------
    ParseError
        If *raw_bytes* cannot be parsed as a MIME message at all.
    """
    # Lazy import to keep the module boundary clean (db.py only).
    from robotsix_auto_mail.db import MailRecord

    try:
        msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
    except Exception as exc:
        raise ParseError("failed to parse raw bytes as MIME message") from exc

    # -- message_id -----------------------------------------------------------
    message_id: str = msg.get("Message-ID", "")
    # Keep brackets as-is per existing convention.

    # -- sender ---------------------------------------------------------------
    sender: str = msg.get("From", "")

    # -- recipients_json ------------------------------------------------------
    to_addresses = email.utils.getaddresses(msg.get_all("To", []))
    cc_addresses = email.utils.getaddresses(msg.get_all("Cc", []))
    recipients_json = json.dumps(
        {
            "to": [addr for _name, addr in to_addresses],
            "cc": [addr for _name, addr in cc_addresses],
        }
    )

    # -- subject (already decoded by policy.default) --------------------------
    subject: str = str(msg.get("Subject", ""))

    # -- date → ISO 8601 via parsedate_to_datetime ----------------------------
    date_str: str = ""
    raw_date: str | None = msg.get("Date")
    if raw_date:
        try:
            dt = email.utils.parsedate_to_datetime(raw_date)
            date_str = dt.isoformat()
        except (ValueError, TypeError, OverflowError):
            date_str = ""

    # -- body & attachments ---------------------------------------------------
    body_plain: str = ""
    body_html: str = ""
    attachments: list[dict[str, Any]] = []

    # msg.walk() visits all parts depth-first; the message itself is first.
    for part in msg.walk():
        if part.is_multipart():
            continue

        content_type = part.get_content_type()
        disposition = part.get_content_disposition()

        # A text part with an explicit attachment disposition is an attachment.
        if disposition == "attachment":
            attachments.append(_attachment_meta(part))
            continue

        if content_type == "text/plain":
            body_plain = _decode_content(part)
        elif content_type == "text/html":
            body_html = _decode_content(part)
        else:
            attachments.append(_attachment_meta(part))

    attachments_json = json.dumps(attachments)

    return MailRecord(
        message_id=message_id,
        sender=sender,
        subject=subject,
        date=date_str,
        imap_uid=imap_uid,
        recipients_json=recipients_json,
        body_plain=body_plain,
        body_html=body_html,
        attachments_json=attachments_json,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _attachment_meta(part: email.message.EmailMessage) -> dict[str, Any]:
    """Extract attachment metadata from a non-text MIME part."""
    filename: str = part.get_filename() or ""
    mime_type: str = part.get_content_type() or "application/octet-stream"

    payload = part.get_payload(decode=True)
    if payload is None:
        size = 0
    elif isinstance(payload, bytes):
        size = len(payload)
    else:
        size = len(str(payload).encode("utf-8"))

    return {"filename": filename, "mime_type": mime_type, "size": size}
