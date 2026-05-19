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

The ``TZDateTime`` type decorator (below) is the **model-level fix**: it
stores all timestamps as naive UTC in the database and re-attaches
``timezone.utc`` on read-back, so application code always sees aware
datetimes.  ``_as_utc()`` remains as a defense-in-depth helper for any
value that bypasses the ORM type machinery.
"""

from datetime import datetime, timezone

from sqlalchemy.types import DateTime, TypeDecorator


class TZDateTime(TypeDecorator):
    """A SQLAlchemy column type that stores aware UTC datetimes as naive
    UTC in the database (compatible with SQLite, which has no native
    timezone support) and re-attaches ``timezone.utc`` on read-back.

    >>> from datetime import datetime, timezone
    >>> tz_dt = TZDateTime()
    >>> stored = tz_dt.process_bind_param(
    ...     datetime(2025, 1, 1, tzinfo=timezone.utc), dialect=None
    ... )
    >>> stored.tzinfo is None
    True
    >>> restored = tz_dt.process_result_value(stored, dialect=None)
    >>> restored.tzinfo == timezone.utc
    True
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is not None:
            if value.tzinfo is None:
                raise TypeError(
                    "TZDateTime requires timezone-aware values; got naive datetime"
                )
            # Convert to UTC and strip tzinfo for storage.
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect):
        if value is not None:
            value = value.replace(tzinfo=timezone.utc)
        return value


def _as_utc(dt: datetime) -> datetime:
    """Coerce a possibly tz-naive datetime to aware UTC.

    Treats naive values as UTC (which they are — we only ever write
    ``datetime.now(timezone.utc)``).  Already-aware values pass through
    unchanged.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
