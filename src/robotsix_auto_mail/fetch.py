"""Watermark-aware IMAP message fetch.

Provides ``fetch_new_messages`` — reads the IMAP UID watermark from the
local datastore, selects the configured folder, searches for only UIDs
beyond the watermark, fetches the raw message bytes, and returns ``(uid,
raw_mime_bytes)`` pairs.  Also provides ``update_watermark`` as a thin
wrapper around ``set_watermark`` with the hardcoded key ``"imap_uid"``.

Both functions take explicit ``sqlite3.Connection`` handles — the
caller controls transactions.  ``fetch_new_messages`` is read-only on
the DB; ``update_watermark`` writes but does not commit (``set_watermark``
handles its own commit).

Depends on: ``robotsix_auto_mail.db``, ``robotsix_auto_mail.imap``,
``robotsix_auto_mail.config``.
"""

from __future__ import annotations

import sqlite3

from robotsix_auto_mail.config import MailConfig
from robotsix_auto_mail.db import get_watermark, set_watermark
from robotsix_auto_mail.imap import ImapClient

# The watermark key is owned by this module — callers never supply it.
_WATERMARK_KEY = "imap_uid"


def fetch_new_messages(
    conn: sqlite3.Connection,
    client: ImapClient,
    config: MailConfig,
) -> list[tuple[int, bytes]]:
    """Fetch raw messages with UIDs beyond the stored watermark.

    Reads the ``"imap_uid"`` watermark from *conn*, selects
    ``config.imap_folder``, searches for UIDs strictly greater than the
    watermark (or ``"ALL"`` on first run), and fetches their raw MIME
    bytes via ``BODY.PEEK[]``.

    This function is **read-only** on the DB: it reads the watermark
    but does not update it.  The caller (pipeline) wraps fetch → parse →
    insert → update-watermark in a single transaction for atomicity.

    Args:
        conn: An open ``sqlite3.Connection`` to the local datastore.
        client: A connected ``ImapClient``.
        config: Mail configuration whose ``imap_folder`` determines
            which mailbox to select.

    Returns:
        A (possibly empty) list of ``(uid, raw_mime_bytes)`` pairs for
        messages that are newer than the stored watermark.
    """
    # 1. Read watermark.
    watermark_raw = get_watermark(conn, _WATERMARK_KEY)

    # 2. Select the configured folder.
    client.select_folder(config.imap_folder)

    # 3. Build search criteria.
    if watermark_raw is not None:
        criteria = f"UID {watermark_raw}:*"
    else:
        criteria = "ALL"

    # 4. Search.
    uids = client.search_uids(criteria)

    # 5. Filter out the watermark UID itself (IMAP ``UID N:*`` is
    #    inclusive).
    if watermark_raw is not None:
        try:
            watermark_uid = int(watermark_raw)
        except (ValueError, TypeError):
            watermark_uid = None
        if watermark_uid is not None:
            uids = [u for u in uids if u > watermark_uid]

    # 6. No new UIDs → nothing to fetch.
    if not uids:
        return []

    # 7. Fetch message bodies.
    return client.fetch_messages(uids)


def update_watermark(conn: sqlite3.Connection, uid: int) -> None:
    """Persist the last-seen IMAP UID so the next run only fetches newer mail.

    Thin wrapper around ``set_watermark`` with the hardcoded key
    ``"imap_uid"``.
    """
    set_watermark(conn, _WATERMARK_KEY, str(uid))
