Stop the worker dispatching the same ticket+stage twice: a per-run claim now guards dispatch (`_pending`
only guarded enqueues), and the active-run map is keyed by `(ticket_id, stage)` so `GET /active` reports
every live run instead of collapsing concurrent runs of one ticket into a single row.
