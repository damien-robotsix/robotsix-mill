# SQLAlchemy + SQLite: DateTime(timezone=True) Silently Ignored

## Limitation

SQLite has no native datetime type. SQLAlchemy stores Python `datetime`
objects as ISO-8601 strings, but **strips `tzinfo` on read-back** when
using the SQLite backend. This means:

```python
# Write: fully aware UTC
ticket.updated_at = datetime.now(timezone.utc)

# Round-trip through SQLite: tzinfo is None
assert ticket.updated_at.tzinfo is None  # naive UTC — silently lost
```

Any comparison between a tz-naive DB value and a tz-aware value raises:

```
TypeError: can't compare offset-naive and offset-aware datetimes
```

## Do NOT prescribe `DateTime(timezone=True)` for SQLite-backed models

Specs that say "use `DateTime(timezone=True)`" for a project using SQLite
are prescribing a parameter that SQLAlchemy silently ignores on that
backend. The spec will look correct but produce broken round-trip
behaviour.

## Canonical workaround: `TZDateTime` TypeDecorator

The project has a `TZDateTime` TypeDecorator in
`src/robotsix_mill/core/datetime_utils.py` that:

1. **On write** (`process_bind_param`): converts any aware datetime to
   naive UTC before storage (`.astimezone(timezone.utc).replace(tzinfo=None)`)
2. **On read** (`process_result_value`): re-attaches `timezone.utc` to
   naive values from the database (`.replace(tzinfo=timezone.utc)`)

This ensures application code always sees timezone-aware datetimes,
regardless of the backend. When writing specs that touch datetime
columns, use this existing `TZDateTime` type instead of raw
`DateTime(timezone=True)`.

If the spec needs a *new* datetime column, prescribe `TZDateTime`
(from `src/robotsix_mill/core/datetime_utils.py`) as the column type.
