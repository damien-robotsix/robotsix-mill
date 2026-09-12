# Retrospect chain guard: operator runbook

When a retrospect follow-up cannot be satisfied by a code change, the
mill must not keep re-filing it generation after generation. The
**retrospect chain guard** is a deterministic (no-LLM) guard that stops
that loop and escalates the decision to a human.

## What is happening

Retrospect detected that an unmet criterion has been re-filed across
multiple generations. The classic shape:

- a ticket closes with its acceptance criterion unmet because the
  criterion is **operational** — it asks to run something against a
  deployed service or on a high-memory host, which a mill implement run
  cannot do (the sandbox is network-less and memory-capped, ~1 GiB);
- retrospect files a fresh follow-up for that criterion;
- the follow-up closes "no change needed" (the criterion still cannot
  be met in the sandbox);
- retrospect files it again, then again — a doomed loop that costs
  implement runs without ever satisfying the criterion.

The guard walks up to **2 generations** of ancestors from the
retrospected ticket. It fires when an ancestor is itself
`source=retrospect` **and** either:

- a history note of that ancestor contains an unmet-criterion marker
  (`did not execute`, `remains outstanding`, `must be executed`,
  `could not be verified`); or
- the proposed follow-up's title is ≥ 0.5 Jaccard-similar to the
  ancestor's title.

When it fires, the guard:

1. **suppresses** the follow-up — no new ticket is created; and
2. posts a comment on the **root ticket** (the original ticket that
   started the chain) with the marker `retrospect chain guard:` and
   routes the root to `human_issue_approval`, so a human decides where
   the verification runs instead of spawning another doomed implement
   run.

A companion gate also catches this class earlier: when a retrospect
follow-up's draft is **ops-shaped** (asks to run against a deployed
service / high-memory host, e.g. mentions `POST /api/…`,
"deployed service", "environment with adequate memory",
"high-memory host"), the refine mechanical fast-path routes it straight
to `human_issue_approval` with note `ops-shaped retrospect follow-up
(matched …) — requires operator routing, not auto-implement` instead of
auto-approving it into implement.

## How to interpret the comment

A `retrospect chain guard:` comment on a root ticket looks like:

```
retrospect chain guard: the acceptance criterion behind the suppressed
retrospect follow-up '<title>' is unverifiable in the mill sandbox
(network-less, memory-capped) and cannot be met by another implement
run. Ancestor chain walked: <root> → <f1> → <f2>. The operator/chat
must decide where the verification runs.
```

The comment identifies an **unmet criterion that code changes alone
cannot satisfy**. The root ticket has been parked in
`human_issue_approval` — normal approve semantics do **not** apply here
(the root is usually already merged/done; this is an ops-routing
decision, not a spec-approval decision).

**Check the ancestor closing notes** to understand why previous
attempts failed. Common patterns:

- `Criterion 'run batch' failed with OOM in sandbox (1 GiB limit), test
  suite fix did not help` — a test-suite change cannot substitute for
  the real run.
- `Criterion requires POST to /api/batch/jobs against deployed service,
  not available in sandbox` — the verification only exists on the
  deployed environment.
- `Criterion requires production-scale load, cannot simulate in sandbox`
  — the sandbox is not representative.

## Decision tree

1. **Can this criterion be addressed by a CODE change that implement
   missed?** → Route the root back to `READY` with guidance on what to
   change (what implement should build/test) rather than re-running the
   same doomed batch.
2. **Is this criterion OPERATIONAL** (requires a deployed service, high
   memory, or production load)? → Execute it manually on the appropriate
   environment and **document the result** on the root ticket, then
   close the root. Do not route it back to implement.
3. **Is the criterion unclear or impossible?** → Close the root with a
   comment explaining what was attempted across the chain and why it
   cannot proceed. Do not re-file it.

This unblocks escalations without re-routing to implement for doomed
attempts.

## How to act on the root ticket

The root sits in `human_issue_approval`. Act via the board or the HTTP
API:

- **Route back to `READY`** (code-change path): `POST /tickets/{id}/approve`
  or the board Approve button — and update the ticket body/comment with
  the guidance implement should follow.
- **Close the root** (operational / impossible paths): `POST
  /tickets/{id}/mark-done` with a note recording what was executed (or
  why it cannot proceed). `mark-done` bypasses the state machine's
  `can_transition` rules, which is required here because the root is
  coming from `DONE` (not a normal pre-implement approval flow).
- **Request changes** (back to `DRAFT` to re-scope): `POST
  /tickets/{id}/request-changes` with a comment.

See [blocked-ticket-recovery.md](blocked-ticket-recovery.md) for the
full `mark-done` / `resume-blocked` recovery workflow and
[approval-gate.md](approval-gate.md) for the approval semantics.

## Example scenarios

- **"Run economy batch (20 games, 2 GiB) against `/api/batch/jobs`,
  deployed service only"** — an Ops task: authorize a human (or the
  chat) to run `POST /api/batch/jobs` against the deployed hexarchy on
  an environment with adequate memory, capture the job id + result on
  the root ticket, then close the root.
- **"Execute acceptance test with production-equivalent load"** — if the
  sandbox cannot simulate that load, close the root noting that test
  suite coverage is sufficient for the sandbox, and deploy + monitor in
  production instead.

## See also

- [index.md](index.md) — documentation home
- [retrospect-memory.md](retrospect-memory.md) — the retrospect agent's
  memory ledger
- [blocked-ticket-recovery.md](blocked-ticket-recovery.md) — recovering
  from BLOCKED / parked tickets
- [approval-gate.md](approval-gate.md) — the human approval gates
- [docs/notify/notifications.md](../notify/notifications.md) — how
  humans are notified when a ticket enters `human_issue_approval`
