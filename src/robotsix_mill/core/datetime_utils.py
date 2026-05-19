"""Timezone-safe datetime helpers.

--------------
Why this module exists
--------------

SQLite has no native datetime type.  SQLModel / SQLAlchemy store Python
``datetime`` objects as ISO-8601 strings but **strip ``tzinfo``** on
read-back.  This means::

    # Write: fully aware
    ticket.updated_at = datetime.now(timezone.utc)

    # Round-trip through SQLite: tzinfo is None
    assert ticket.updated_at.tzinfo is None  # naive UTC

Any comparison between a tz-naive DB value and a tz-aware value (e.g., a
freshly-built ``lookback_cutoff``) raises::

    TypeError: can't compare offset-naive and offset-aware datetimes

The helper below is the central, documented workaround.  **Always** pass
any ``datetime`` that may have come from the DB through ``_as_utc()``
before comparing it against an aware value.
"""

from datetime import datetime, timezone


def _as_utc(dt: datetime) -> datetime:
    """Coerce a possibly tz-naive datetime to aware UTC.

    Treats naive values as UTC (which they are — we only ever write
    ``datetime.now(timezone.utc)``).  Already-aware values pass through
    unchanged.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
