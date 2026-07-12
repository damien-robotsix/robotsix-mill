# Dedup / already-done guard

Before the expensive refine agent runs, a single cheap LLM call checks
whether the draft is (a) a duplicate of an existing ticket, or (b)
already implemented in a recent commit. When a clear match is found, the
ticket is short-circuited straight to `CLOSED` — no refiner, no approval
gate, no wasted cost. This prevents re-proposing the same gap or a
change that was already merged.

The check is **conservative**: it only flags clear matches (same
intent/change, not merely the same area) and degrades gracefully on
failure. It inspects recent commits on the forge target branch
(`MILL_DEDUP_LOOKBACK_COMMITS`, default 20) and recently-closed tickets
(`MILL_DEDUP_LOOKBACK_DAYS`, default 30 days).

| Variable | Default | Description |
|---|---|---|
| `MILL_DEDUP_MODEL` | `deepseek/deepseek-v4-pro` | Model for the dedup check |
| `MILL_DEDUP_REQUEST_LIMIT` | `4` | Per-call request cap (kept tight) |
| `MILL_DEDUP_LOOKBACK_DAYS` | `30` | Days back to consider closed tickets as dup candidates |
| `MILL_DEDUP_LOOKBACK_COMMITS` | `20` | Recent commits to inspect for "already done" |

Implemented in `agents/dedup.py:run_dedup_check`.

## See also

- [index.md](index.md) — documentation home
- [docs/config/configuration.md](config/configuration.md) — full env-var reference
