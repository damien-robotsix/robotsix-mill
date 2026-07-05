# Notifications

When a ticket enters a human-attention state — `human_issue_approval`,
`human_mr_approval`, `blocked`, or `errored` — the worker fires a best-effort
push notification via [ntfy.sh](https://ntfy.sh) so you know to
intervene without watching the board.

Configure with two environment variables:

| Variable | Description |
|---|---|
| `NTFY_URL` | Full ntfy topic URL, e.g. `https://ntfy.sh/mytopic`. Leave blank to disable (the default). |
| `NTFY_TOKEN` | Optional bearer token sent as `Authorization: Bearer <token>`. |

Notification delivery is fire-and-forget: network errors and timeouts are
logged at warning level and never interfere with ticket processing. Only
worker-driven transitions trigger notifications — API/CLI transitions
(e.g. manual approve) do not.

The four trigger states are defined in `notify.py:_TRIGGER_STATES`:
`HUMAN_ISSUE_APPROVAL`, `HUMAN_MR_APPROVAL`, `BLOCKED`, `ERRORED`.

## See also

- [index.md](index.md) — documentation home
- [docs/config/configuration.md](config/configuration.md) — full env-var reference
