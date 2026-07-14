# Epic-decomposition pre-filing dedup

When an epic is decomposed into child tickets, an **advisory** dedup
check runs before each child is filed. It flags a would-be child that
duplicates work already covered elsewhere, so the duplicate is caught
cheaply during the child's own refine cycle instead of consuming a full
implement pass.

This is **not** the same as the pre-refine
[dedup guard](dedup-guard.md). That guard runs a cheap LLM call before
the refine agent and can short-circuit a draft straight to `CLOSED`.
The epic-decomposition check is purely mechanical (no LLM call), only
**annotates** a child's body with a warning, and **never drops** a
child.

## The two duplicate classes it catches

1. **Concurrent independent ticket** — a recently shipped or in-flight
   ticket already covers the child's scope. Detected by the
   recent-ticket check (`find_prior_matching_ticket`): path-like tokens
   are extracted from the child body as `target_files` and the child
   title is used as the fingerprint, then candidates filed within the
   recency window are matched by verbatim file path (in their body) or
   normalized-title overlap.

2. **In-batch sibling overlap** — two children proposed in the *same*
   decomposition batch overlap on an extracted file path or a
   normalized title. Each child is compared against the siblings already
   accepted earlier in the batch.

## Advisory nature

An overlap is surfaced by prepending a `> [!warning]` advisory block to
the child's body (via `annotate_child_body`). The child is still filed;
its own refine cycle sees the flag and can verify and close-as-duplicate
cheaply if confirmed.

Overlaps are **logged, never silently suppressed**, and a child is
**never dropped** — scope boundaries can legitimately shift during
implementation, so the decomposer (and the child's refine cycle) makes
the final call.

The check is **best-effort**: any internal failure logs and yields no
flags, so children are still filed even if the dedup pass errors.

## The `epic_dedup_lookback_days` setting

`epic_dedup_lookback_days` (default `7` days) is the recency window for
the recent-ticket check — only tickets filed within that many days back
are considered as duplicate candidates. It mirrors
`trace_review_dedup_lookback_days` but is an independent setting so the
epic-decomposition policy can diverge from the trace-review policy.

## `exclude_ids` filtering

The parent epic and its already-existing children are passed as
`exclude_ids` to the recent-ticket match, so they are skipped as
candidates and a child never self-matches against its own epic or its
siblings already on the board.

## Where it runs

The check runs at all three child-filing call sites:

- `stages/refine.py` — the promote-to-epic branch.
- `runtime/worker.py` — `_run_epic_reprocess`.
- `runtime/routes/_epics.py` — the `/generate-children` route.

## Implementation pointer

The primitives live in `src/robotsix_mill/dedup.py`
(`find_child_overlaps`, `find_prior_matching_ticket`,
`annotate_child_body`); tests in `tests/test_dedup.py`.

## See also

- [index.md](index.md) — documentation home
- [dedup-guard.md](dedup-guard.md) — pre-refine duplicate / already-done check
- [docs/config/configuration.md](config/configuration.md) — full env-var reference
