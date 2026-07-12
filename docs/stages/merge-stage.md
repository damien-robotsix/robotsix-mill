# Merge stage: gate-check, auto-rebase, CI-fix & review-revision

The merge stage monitors open PRs and handles several scenarios:
gate-checking new PRs before notifying humans, auto-rebasing conflicting
PRs, auto-fixing failing CI, and autonomously addressing human reviewer
change requests.

## Gate-check: `IMPLEMENT_COMPLETE` → `HUMAN_MR_APPROVAL`

After the deliver stage creates a PR, the ticket enters `IMPLEMENT_COMPLETE`
instead of `HUMAN_MR_APPROVAL`.  The merge stage polls this new state and
verifies two gates before promoting the ticket:

1. **CI is green** — the PR's CI checks must all pass.
2. **PR is mergeable** — no conflicts with the target branch.

When both gates pass, the ticket transitions to `HUMAN_MR_APPROVAL`
(triggering an ntfy notification to the human).  If either gate fails:

- **Failing CI** → `FIXING_CI` (auto-fix agent runs next poll).
- **Conflicting PR** → `REBASING` (rebase agent runs next poll).
- After auto-fix, the ticket returns to `IMPLEMENT_COMPLETE` so both
  gates are re-verified before another human notification.

This means humans are only notified when the PR is actually ready for
review and merge — no premature noise for PRs with failing CI or
conflicts.

## Silent fallback from `HUMAN_MR_APPROVAL`

If a ticket is already in `HUMAN_MR_APPROVAL` (gates passed) and the
merge stage later detects CI failure or a merge conflict, it silently
transitions back to `IMPLEMENT_COMPLETE`.  No ntfy notification is
fired — the ticket leaves the human-review state and the robot
attempts auto-fix.  The human is only re-notified when both gates
pass again.

## Auto-rebase

The rebase agent (`agents/rebasing.py`) resolves git rebase conflicts
using the LLM. It is invoked from two paths:

1. **Implement-stage defensive rebase** — before the implement agent
   runs, `try_rebase_onto` rebases the ticket branch onto the latest
   remote target. If that rebase fails (conflict), the ticket transitions
   to `REBASING` instead of blocking.

2. **Stale-PR conflict** — when a PR sits in `IMPLEMENT_COMPLETE`
   or `HUMAN_MR_APPROVAL` while other PRs merge onto the target branch, it
   may become stale and develop merge conflicts. The forge's `mergeable`
   flag drives this detection.

In both paths, once the rebase agent runs:

- On success the ticket branch is force-pushed and the ticket returns to
  `IMPLEMENT_COMPLETE` for gate re-verification.
  - If no PR exists yet (implement-stage path), the ticket routes to
    `READY` and re-enters the implement stage on the next worker tick.
- On failure (after exhausting retries) the ticket escalates to
  `BLOCKED` (resumable) — no half-rebased state is ever pushed.

| Variable | Default | Description |
|---|---|---|
| `MILL_REBASE_MAX_ATTEMPTS` | `5` | Max rebase attempts per ticket before escalating to BLOCKED. Each attempt is one LLM invocation. |

The rebase agent uses the same sandboxed shell + file tools as the
implement agent, scoped to the ticket's clone. It never pushes, opens
PRs, or interacts with the forge.

### Multi-repo conflict handling

A multi-repo ticket may have conflicts on individual repos while others
remain mergeable. The merge stage handles this by running the rebase
agent **inline** during the polling loop, one conflicting repo per poll
(bounded by a per-repo attempt counter).

Unlike the single-repo path (which uses the `REBASING` state), a
multi-repo ticket has a single state (`IMPLEMENT_COMPLETE`), so conflict
recovery must run inline:

- When one or more repos report `mergeable: False`, the merge stage
  invokes the rebase agent on the first conflicting repo's workspace
  clone.
- On success the rebased branch is force-pushed to that repo's remote,
  and the ticket remains in `IMPLEMENT_COMPLETE` to re-poll.
- The remaining conflicting repos are picked up on the next poll (one at
  a time, so agents can stabilize the repo between attempts).
- On failure after exhausting retries (per-repo attempt counter) the
  ticket escalates to `BLOCKED` (resumable).

This mirrors the single-repo path but runs synchronously during the poll
cycle rather than as a deferred state machine. The rebase attempt counter
is tracked **per-repo** (e.g., `rebase_repo-b.count`) so each repo gets a
fresh budget if it later becomes conflicting again.

## Auto-fix of failing remote CI

When a PR has **failing** remote CI checks (GitHub Actions), the merge
stage transitions the ticket to `FIXING_CI` and invokes a **ci-fix
agent** (`agents/ci_fixing.py`) that analyses the failing check-run
output and applies minimal fixes.

- The forge adapter fetches check-run status (and falls back to the
  combined commit-statuses API for older repos).
- If a PR has failing CI, the merge stage transitions to `FIXING_CI`
  (from either `IMPLEMENT_COMPLETE` or `HUMAN_MR_APPROVAL`).
- The CI-fix stage invokes `run_ci_fix_agent` on the ticket's workspace
  clone, passing it a summary of the failing checks and file-level
  annotations.
- On success the ticket branch is force-pushed (the ticket goes back to
  `IMPLEMENT_COMPLETE` for the next poll to re-verify both gates).
- On failure the ticket escalates to `BLOCKED` (resumable) — no
  half-fixed state is ever pushed.

| Variable | Default | Description |
|---|---|---|
| `MILL_CI_FIX_MAX_ATTEMPTS` | `2` | Max CI-fix attempts per ticket before escalating to BLOCKED. Each attempt is one LLM invocation. |

The ci-fix agent uses the same sandboxed shell + file tools as the
implement agent, scoped to the ticket's clone. It never pushes, opens
PRs, or interacts with the forge. The agent does **NOT** have web-search
access — it works only from the failing summary and local files.

### Stale-branch refresh (out-of-scope failures)

When the ci-fix agent classifies a failure as **out-of-scope** (i.e., the
failing check cannot be fixed by changes in this ticket's diff), the stage
first checks whether the PR branch is behind its base. If the branch is
stale, a fix that landed on the target branch while the ci-fix agent was
running may have already resolved the failure. In this case, the stage
calls the forge's `update_branch` API to merge the target branch into the
PR branch server-side, records a history note, and returns to
`IMPLEMENT_COMPLETE` to re-poll CI.

This **stale-branch refresh** happens at most once per out-of-scope cycle
(tracked by an internal counter). If the failure still exists after a
refresh, the stage proceeds to spawn a dedicated dependency-fix ticket as
before.

- The refresh is deterministic and non-fatal — it never blocks forward
  progress and gracefully falls back to the normal out-of-scope spawn if
  the API call fails.
- This optimization reduces spurious dependency-fix tickets on fast-moving
  target branches where fixes land frequently.

## Auto-fix of review feedback (opt-in)

When `MILL_REVIEW_FEEDBACK_ENABLED=true` and a human reviewer submits a
"request changes" review on the PR, the merge stage detects the
`CHANGES_REQUESTED` state and transitions the ticket to
`ADDRESSING_REVIEW`. On the next poll, it invokes a **review-revision
agent** (`agents/review_revision.py`) that reads the review comments,
makes the requested code changes, runs local tests, and commits.

- The forge adapter queries the PR's review status via
  `pr_review_status()`. Only GitHub is supported — GitLab always
  returns `PENDING`, so the feedback loop never triggers.
- If the review has body text or line comments, the comments are
  persisted as `review_feedback.json` in the ticket's artifacts
  directory so the agent can read them even if the forge becomes
  unreachable on the next poll.
- The review-revision agent is invoked on the ticket's workspace clone
  with the formatted comments and changed file list.
- On success the ticket branch is force-pushed (the ticket goes back to
  `HUMAN_MR_APPROVAL` for human re-review).
- On failure with retries remaining the ticket stays in
  `ADDRESSING_REVIEW` for the next poll.
- On failure after exhausting retries the ticket escalates to `BLOCKED`
  (resumable) — no half-fixed state is ever pushed.
- If the review has an empty body and no line comments, it is treated
  as a no-op and the ticket stays in `HUMAN_MR_APPROVAL`.

| Variable | Default | Description |
|---|---|---|
| `MILL_REVIEW_FEEDBACK_ENABLED` | `false` | Enable autonomous review-revision agent (opt-in). |
| `MILL_REVIEW_REVISION_MAX_ATTEMPTS` | `2` | Max review-revision LLM invocations per ticket before escalating to BLOCKED. |
| `MILL_REVIEW_REVISION_MODEL` | `deepseek/deepseek-v4-pro` | Model for the review-revision agent. |

The review-revision agent uses the same sandboxed shell + file tools as
the implement agent, scoped to the ticket's clone. It never pushes,
opens PRs, or interacts with the forge. The agent does **NOT** have
web-search access — it works only from the review comments and local
files. The agent's system prompt explicitly forbids gate weakening
(removing test assertions, lint rules, or security checks).

## State flow summary

```
DELIVERABLE
    │ (deliver stage opens PR)
    ▼
IMPLEMENT_COMPLETE  ←────────────────────────────────┐
    │ (merge stage polls gates)                       │
    ├── CI green + mergeable → HUMAN_MR_APPROVAL      │
    ├── CI failing            → FIXING_CI ────────────┤
    ├── conflicting           → REBASING ─────────────┤
    └── CI pending            → IMPLEMENT_COMPLETE (wait)
                                                        │
HUMAN_MR_APPROVAL                                      │
    │ (merge stage re-polls)                           │
    ├── merged               → DONE                    │
    ├── closed unmerged      → BLOCKED                │
    ├── CI failing           → IMPLEMENT_COMPLETE ─────┘
    ├── conflicting          → IMPLEMENT_COMPLETE ─────┘
    ├── changes requested    → ADDRESSING_REVIEW ──────┐
    ├── CI green + eligible  → DONE (auto-merge)       │
    ├── CI pending + eligible → WAITING_AUTO_MERGE     │
    └── CI green + not eligible → HUMAN_MR_APPROVAL (wait)
                                                       │
ADDRESSING_REVIEW                                      │
    │ (merge stage runs review-revision agent)         │
    ├── agent success        → HUMAN_MR_APPROVAL ──────┘
    ├── agent retry          → ADDRESSING_REVIEW
    └── agent exhausted      → BLOCKED
```

## See also

- [index.md](index.md) — documentation home
- [docs/configuration.md](configuration.md) — full env-var reference
- [docs/agents/index.md](agents/index.md) — agent catalog
