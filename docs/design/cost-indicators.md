# Cost Indicators — Design Specification

> **Status:** draft  
> **Epic:** cost-dashboard-improvements  
> **Scope:** design only — no code changes in this ticket

## Overview

This document specifies three new cost indicators for the robotsix-mill
management board.  Each indicator answers a question that the current
per-agent total-cost bar chart cannot answer.  The three indicators are
additive: the existing agent bar chart, grand-total summary line, and
lookback selector are preserved as-is.

All three indicators derive their data from Langfuse traces via the
existing public API surface (`/api/public/traces` with filters,
`/api/public/traces/{id}`).  No new Langfuse endpoints are required.

---

## Prelude: dual selection mode

Child 2 of this epic will replace the time-based lookback selector
(`?lookback_hours=N`) with a trace-count control (`?trace_count=N`).
Each indicator specification below declares its behaviour under **both**
selection modes so that child 3 can implement the indicator independent
of which mode is active.

| Mode | Parameter | Semantics |
|------|-----------|-----------|
| Time-based | `?lookback_hours=N` | All traces with `timestamp >= now - N hours` |
| Trace-count | `?trace_count=N` | The `N` most-recent traces across all agents |

---

## Indicator 1: Cost Trend Sparkline

**Insight answered:** Is cost trending up or down?

### Formula

For each time bucket *B* covering the selected window:

```
SUM(trace.totalCost WHERE trace.timestamp ∈ B)
```

**Bucket granularity:**

| Lookback | Bucket width |
|----------|-------------|
| ≤ 24 h   | 1 hour      |
| > 24 h   | 1 day       |

**Trace-count mode variant:** When selection is "last N traces" the
x-axis is trace ordinal (1 … N) rather than wall-clock time.  Buckets
are equal-width trace-count bands:

- `N ≤ 100`  → `ceil(N / 10)` traces per band (capped at 10 bands)
- `N > 100`  → `ceil(N / 20)` traces per band (capped at 20 bands)

Within each band, `SUM(trace.totalCost)`.  The y-axis remains cost;
only the x-axis changes from time to ordinal.

### Required data fields

| Field | Source | Notes |
|-------|--------|-------|
| `trace.timestamp` | Langfuse `/api/public/traces` | Already queried |
| `trace.totalCost` | Langfuse `/api/public/traces` | Already queried |

**New endpoint required:** `GET /costs/trend`

Parameters:

- Time-based: `?lookback_hours=N` (clamped 1–168)
- Trace-count (child 2): `?trace_count=N` (clamped 1–500)

Response shape:

```json
{
  "buckets": [
    {"ts": "2025-06-24T00:00:00Z", "total_cost": 0.1234, "trace_count": 5},
    {"ts": "2025-06-24T01:00:00Z", "total_cost": 0.0567, "trace_count": 3}
  ]
}
```

- `ts` — ISO-8601 start of the bucket (time-based) or `"1-50"` style
  label (trace-count mode).
- `total_cost` — sum of `totalCost` for all traces in that bucket,
  already aggregated server-side.  No individual traces sent to the
  browser.
- `trace_count` — number of traces in the bucket (for tooltip detail).
- `buckets` sorted chronologically (time-based) or ordinally
  (trace-count).

**Feasibility gate:** `GET /api/public/traces` supports
`fromTimestamp`, `orderBy=timestamp.desc`, and pagination.  The backend
already paginates this endpoint for `list_recent_traces()` in
`langfuse/client.py`.  The trend endpoint follows the same pattern,
fetching all traces in the lookback window (bounded at ~500 for perf)
and bucketing in Python.

### Visualization

```
┌──────────────────────────────────────────────────────────┐
│ 💰 Cost Dashboard                                        │
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │  ▄▄▄▄▄  ▄▄▄▄▄  ▄▄▄▄▄  ▄▄▄▄▄  ▄▄▄▄▄  ▄▄▄▄▄▄▄▄▄   │  │  ← sparkline
│  │  █████▄ ██████ ██████ ██████ ██████ ███████████   │  │     area/bar
│  │  ██████▌██████▌██████▌██████▌██████▌███████████▌  │  │     60–80 px
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│  4 agents · $0.2345 total  |  Avg $0.0156 / trace       │  ← summary + avg tile
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │  implement  ████████████████████  $0.1200          │  │  ← existing bar chart
│  │  review     ██████████            $0.0600          │  │
│  │  refine     ████████              $0.0400          │  │
│  │  retrospect ██                    $0.0145          │  │
│  └────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────┘
```

**Placement:** Above the agent bar chart, below the lookback/time-count
selector.  This puts the time-series context before the aggregate
breakdown — the operator reads top-to-bottom: trend → totals → per-agent
detail.

**Dimensions:**
- Height: 60–80 px (configurable CSS `min-height` / `max-height`).
- Width: fills the drawer content area (constrained by `.cost-bar-row`
  flexbox, ~700 px in the 900 px drawer minus padding).

**Rendering:**
- Use an HTML `<canvas>` element with id `cost-sparkline`.
- Background fill: `#1a1e27` (matching `.cost-bar-track`).
- Line / fill area: `#3b82f6` at 50 % opacity (first agent color from
  `colors` array in `board.js`).
- Tooltip: a single `title` attribute on the canvas or a JS-driven
  `div` overlay on hover/tap showing `$X.XXXX` and bucket label.
- Empty state: render placeholder text "No trend data available for
  this period." in muted `#7d828c`, centered in the canvas area.
- No axis labels, no gridlines — keep the sparkline minimal.  The tooltip
  provides precise values on demand.

**CSS class reference (from `board.css`):**

| Element | Existing class | Color |
|---------|---------------|-------|
| Track background | `.cost-bar-track` | `background:#1a1e27` |
| Muted text | `.muted` | `color:#7d828c` |
| Accent (line) | N/A (new) | `#3b82f6` |

---

## Indicator 2: Cumulative Ticket Cost

**Insight answered:** What is the *true* cost of an epic or any ticket
with child tickets?

### Formula

```
cumulative_cost(ticket_id) =
    session_cost(ticket_id)
  + Σ session_cost(descendant.id)   for each descendant in _all_descendants(ticket_id)
```

- `session_cost` is the existing Langfuse session-total lookup in
  `langfuse/client.py` (with 60 s in-memory TTL).
- `_all_descendants` is the existing BFS walker in `core/service.py:438`.
- The implementation (`cumulative_cost`) already exists at
  `core/service.py:410`.

**Trace-count mode compatibility:** Cumulative cost is independent of
the dashboard lookback/trace-count selector.  It always sums the full
session history regardless of how the board filters or displays tickets.
No adaptation needed.

### Required data fields

| Field | Source | Notes |
|-------|--------|-------|
| Ticket `id` + `parent_id` tree | SQLite `ticket` table | Already in DB; `_all_descendants` walks it |
| Per-session cost | `session_cost` / `session_cost_cached` | Already implemented; 60 s TTL cache |

No new endpoint is required.  The existing `cumulative_cost` service
method is wired into the ticket-read pipeline (`enrich_ticket_read`)
with a blocking/non-blocking split:

| Context | Blocking | Cost function |
|---------|----------|---------------|
| `/tickets` list (polled every 1 s) | `False` | `session_cost_cached` for all tree nodes |
| `/tickets/{id}` drawer | `True` | `session_cost` — authoritative Langfuse HTTP per uncached session |

### Data model change

Currently `TicketRead.cost_usd` is a single float.  For epics it is
overwritten with cumulative cost in `enrich_ticket_read`; for non-epics
it is direct session cost.  This conflates two distinct numbers.

**Proposed:** Add an optional `cumulative_cost` field to `TicketRead`:

```python
class TicketRead(SQLModel):
    ...
    cost_usd: float              # always direct session cost (unchanged for non-epics)
    cumulative_cost: float | None = None  # present when descendants exist and > direct
```

Behaviour per ticket type:

| Ticket type | `cost_usd` | `cumulative_cost` |
|-------------|-----------|-------------------|
| Leaf ticket (no children) | direct session cost | `None` or `== cost_usd` |
| Ticket with children (non-epic) | direct session cost | recursive sum |
| Epic | direct session cost | recursive sum (was `cost_usd` before) |

The epic special-case in `enrich_ticket_read` changes from overwriting
`cost_usd` to populating `cumulative_cost` for *all* tickets that have
descendants, not just epics.

**Feasibility gate:** `cumulative_cost()` already exists, `_all_descendants()`
already exists, `session_cost`/`session_cost_cached` already exist.  The
only change is plumbing a second float through `TicketRead` and adjusting
the epics special-case.

### Visualization — ticket cards

```
┌──────────────────────────────────────────┐
│  Add retry logic to implement agent      │
│  abc123de                                │
│  user                      $0.0123/0.0456│  ← split badge
│  ⏺ implementing…                         │
└──────────────────────────────────────────┘
```

- When `cumulative_cost` is `None` or equals `cost_usd`: render the
  existing `.cost` span unchanged (`$${(t.cost_usd||0).toFixed(4)}`).
- When `cumulative_cost > cost_usd`: render a split badge:
  ```
  <span class="cost">$${direct.toFixed(4)}</span><span class="cost-cumulative">/$${cum.toFixed(4)}</span>
  ```
  - Direct cost: existing `.cost` styling (`font-size:10px; color:#7d828c`).
  - Cumulative cost: new class `.cost-cumulative` at `color:#aab0bd`
    (brighter, but not attention-grabbing).  The forward-slash separator
    is plain text within the span.

**CSS additions (in `board.css`):**

```css
.cost-cumulative{font-size:10px;color:#aab0bd}
```

### Visualization — ticket drawer

In the `open_()` detail pane, after the existing cost line:

```
· cost $0.0123
· cumulative (incl. children) $0.0456
```

- The cumulative line is **omitted** when `cumulative_cost` is `None`
  or equals `cost_usd` (no descendants or descendants have zero cost).
- Styling: same `<b>` tag as the direct cost line, inside the existing
  `<p>` block.  Use literal text `cumulative (incl. children)` in muted
  `#7d828c`.

---

## Indicator 3: Average Trace Cost

**Insight answered:** How efficient is each trace?  Are we running many
cheap traces or few expensive ones?

### Formula

```
avg_trace_cost = grand_total_cost / total_trace_count
```

Where:

```
grand_total_cost   = Σ entry.total_cost    across all entries in /costs/by-agent response
total_trace_count  = Σ entry.trace_count   across all entries in /costs/by-agent response
```

Both values are already returned by `GET /costs/by-agent`.  No new
endpoint is required — this is a pure client-side computation.

**Trace-count mode compatibility:** The same `/costs/by-agent` endpoint
(or its trace-count analogue from child 2) continues to return
`total_cost` and `trace_count` per agent.  The client-side division is
unchanged.  The average is recomputed on every `renderCostDashboard()`
call.

### Required data fields

| Field | Source | Notes |
|-------|--------|-------|
| `total_cost` | `GET /costs/by-agent` response | Already returned per agent |
| `trace_count` | `GET /costs/by-agent` response | Already returned per agent |

### Visualization

Placement: inline on the same row as the existing `.cost-summary` line,
separated by a visual divider.

```
4 agents · $0.2345 total  |  Avg $0.0156 / trace
^^^^^^^^^^^^^^^^^^^^^^^^     ^^^^^^^^^^^^^^^^^^^^
existing .cost-summary        new .cost-avg-tile
```

**DOM structure:**

```html
<div class="cost-summary-row">
  <span class="cost-summary">4 agents · $0.2345 total</span>
  <span class="cost-summary-divider">|</span>
  <span class="cost-avg-tile">Avg <span class="cost-avg-value">$0.0156</span> / trace</span>
</div>
```

**CSS (in `board.css`):**

```css
.cost-summary-row{display:flex;align-items:center;gap:8px;margin-bottom:10px}
.cost-summary{color:#7d828c;font-size:11px}
.cost-summary-divider{color:#3a3f4a;font-size:11px}
.cost-avg-tile{font-size:12px;color:#cfd3db}
.cost-avg-value{color:#eef0f4;font-variant-numeric:tabular-nums}
```

- `.cost-avg-value` uses `font-variant-numeric:tabular-nums` (matching
  `.cost-bar-amount`) so the dollar figure has stable digit widths
  across renders.
- The divider `|` uses the subtle border color `#3a3f4a` so it doesn't
  compete with the data.

**Empty state:** When `total_trace_count == 0`:

```
4 agents · $0.0000 total  |  Avg — / trace
```

Render `Avg — / trace` in muted `#7d828c` (remove the `.cost-avg-value`
wrapper so the whole string is muted).

---

## Acceptance criteria checklist

- [ ] Document committed at `docs/design/cost-indicators.md`.
- [ ] Each indicator has: purpose, formula, data fields, trace-count
      compatibility, and visual mockup.
- [ ] Every required data field is traced to an existing Langfuse API
      field and an existing code path.
- [ ] No indicator requires data the system does not already retrieve.
- [ ] No implementation code in this document — formulas, shapes,
      CSS class names, and endpoint signatures only.

---

## Out of scope (non-goals for the implementation ticket)

- Token-level or model-level cost granularity — the codebase does not
  read per-observation `usage`/`model` fields from trace detail.
- Changes to the existing agent bar chart layout or styling.
- Persisting cost in the `ticket` table — `cost_usd` remains a read-time
  enrichment.
- Real-time push updates — the existing 1 s poll cycle is sufficient.
- Export / CSV / alerting on cost thresholds.
