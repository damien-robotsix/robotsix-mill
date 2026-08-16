# Notifications

When a ticket enters a human-attention state — `human_issue_approval`,
`human_mr_approval`, `blocked`, or `errored` — the worker fires a
best-effort notification so you know to intervene without watching the
board.

## Fleet notification (preferred)

The fleet notification endpoint is the primary delivery channel. It sends
structured JSON payloads to a configurable endpoint (e.g. robotsix-chat),
with severity, category, dedup key, and suppressed-count fields so the
sink can display a digest rather than flooding the channel.

Configure via the `secrets:` block of `config/config.json`:

| Secret key | Description |
|---|---|
| `fleet_notify_url` | Fleet notification endpoint URL, e.g. `https://fleet.example.com/notify`. Leave blank to disable (the default). |
| `fleet_notify_token` | Optional bearer token sent as `Authorization: Bearer <token>`. |

### Payload shape

Every notification POST sends a JSON body with these fields:

| Field | Type | Description |
|---|---|---|
| `source` | `string` | Always `"mill"`. |
| `severity` | `string` | `"critical"` (BLOCKED), `"warning"` (ERRORED), or `"info"` (human_approval). |
| `category` | `string` | `"blocked"`, `"errored"`, `"human_approval"`. |
| `ticket_id` | `string` | Mill ticket ID. |
| `ticket_title` | `string` | Ticket title. |
| `state` | `string` | Mill state value (e.g. `"blocked"`). |
| `note` | `string` | Human-readable note, may be empty. |
| `board_url` | `string` | Mill board API base URL. |
| `dedup_key` | `string` | Suppression group (`"{state}:{board_url}"`). |
| `suppressed_count` | `integer` | Number of identical notifications suppressed since the last delivery. |
| `timestamp` | `string` | ISO-8601 UTC timestamp of the notification. |

### Deduplication & rate limiting

When the same `(state, board_url)` fires multiple times within a 60-second
window, only the first notification is sent immediately. Subsequent
identical notifications are counted in `suppressed_count`, and the next
delivery after the cooldown includes the total suppressed count. This
prevents a sweep of 41 blocked tickets from becoming 41 pushes — the
sink receives one notification with `suppressed_count: 40`.

## Legacy ntfy fallback

If `fleet_notify_url` is unset but `ntfy_url` is configured, mill falls
back to plain-text [ntfy.sh](https://ntfy.sh) notifications.  This
path is maintained for existing deployments that have not yet migrated.

| Env var | Description |
|---|---|
| `NTFY_URL` | Full ntfy topic URL, e.g. `https://ntfy.sh/mytopic`. Leave blank to disable (the default). |
| `NTFY_TOKEN` | Optional bearer token sent as `Authorization: Bearer <token>`. |

## Behaviour

Notification delivery is fire-and-forget: network errors and timeouts are
logged at warning level and never interfere with ticket processing. Only
worker-driven transitions trigger notifications — API/CLI transitions
(e.g. manual approve) do not.

The four trigger states are defined in `notify.py:_TRIGGER_STATES`:
`HUMAN_ISSUE_APPROVAL`, `HUMAN_MR_APPROVAL`, `BLOCKED`, `ERRORED`.

## See also

- [docs/config/configuration.md](../config/configuration.md) — full config reference
- [src/robotsix_mill/notify/fleet.py](../../src/robotsix_mill/notify/fleet.py) — fleet notifier implementation