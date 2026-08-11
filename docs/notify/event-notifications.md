# Event notifications (webhook)

Every ticket state transition produces an HTTP POST notification to
configured subscriber URLs — the **event notification webhook**.  This
lets external components (e.g. a chat subsession monitor) react to
state changes without polling the board.

## Subscribing

Configure one or more subscriber URLs in `config.yaml`:

```yaml
subscriber_urls:
  - "https://chat.example.com/api/mill-events"
subscriber_shared_secret: "a-random-secret"  # optional
```

Or via environment variables:

| Variable | Description |
|---|---|
| `MILL_SUBSCRIBER_URLS` | JSON array of subscriber endpoint URLs (e.g. `'["https://..."]'`). |
| `MILL_SUBSCRIBER_SHARED_SECRET` | Optional shared secret added as `X-Mill-Event-Secret` header. |

The subscriber URL list is reloaded on restart.  A running mill does
**not** hot-reload subscriber URLs — restart the worker to pick up
changes.

## Payload

```json
{
  "ticket_id": "20250101T000000Z-example-ticket-ab12",
  "board_id": "my-board",
  "old_state": "ready",
  "new_state": "code_review",
  "timestamp": "2025-01-01T12:00:00.123456+00:00"
}
```

| Field | Type | Description |
|---|---|---|
| `ticket_id` | string | Full ticket identifier. |
| `board_id` | string | The board the ticket belongs to. |
| `old_state` | string | State before the transition. |
| `new_state` | string | State after the transition (the current state). |
| `timestamp` | string | ISO-8601 UTC timestamp of the transition. |

## Delivery semantics

- **HTTP method:** `POST` with `Content-Type: application/json`.
- **Authentication:** when `subscriber_shared_secret` is configured,
  the request includes an `X-Mill-Event-Secret` header.  Subscribers
  should verify this header to reject spoofed events.
- **Best-effort, asynchronous.**  Each delivery spawns a daemon thread;
  the caller (the state-transition code) returns immediately — a slow
  or dead subscriber never blocks ticket processing.
- **Retries:** up to 3 attempts with exponential backoff (1 s base,
  10 s cap).  After the final attempt the event is **silently
  dropped** (logged at warning level).
- **Ordering:** events are delivered concurrently (one thread per
  subscriber URL) and may arrive out of order.  Subscribers should
  use the `timestamp` field for ordering, not arrival time.

## Reconciliation (poll fallback)

Because the webhook is best-effort, a subscriber that misses an event
(e.g. due to a restart or network blip) can reconcile by polling
`GET /tickets?updated_after=<timestamp>`.  This returns every ticket
whose `updated_at` is strictly after the given ISO-8601 UTC instant,
allowing the subscriber to catch up on missed transitions.

Typical reconciliation flow:

1. Subscriber records the `timestamp` of the last successfully
   processed event.
2. On startup, or periodically, it calls
   `GET /tickets?updated_after=<last_seen_timestamp>`.
3. It processes any tickets in the response that it hasn't seen yet.

## See also

- [notifications.md](notifications.md) — human-attention push
  notifications (ntfy.sh).
- [index.md](index.md) — documentation home.
- [docs/config/configuration.md](../config/configuration.md) — full
  config reference.
