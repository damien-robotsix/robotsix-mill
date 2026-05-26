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

## Auto-fix of failing remote CI

When a PR has **failing** remote CI checks (GitHub Actions), the merge
stage transitions the ticket to `FIXING_CI`.  Before invoking the
expensive ci-fix agent it runs a **categorizer gate**
(`agents/ci_fixing.py:categorize_ci_failure()`) — a single cheap LLM
inference that classifies the failure summary into one of six
categories:

| Category | Meaning | Action |
|---|---|---|
| `test_failure` | Test assertions failed | **Fixable** — proceed with ci-fix agent |
| `type_error` | Type checker (mypy/pyright/tsc) failed | **Fixable** — proceed with ci-fix agent |
| `lint_error` | Linter/formatter (ruff/eslint/prettier) failed | **Fixable** — proceed with ci-fix agent |
| `build_error` | Compilation or Docker build failed | **Fixable** — proceed with ci-fix agent |
| `env_error` | Infra, rate-limit, secret, permission, or external service failure | **NOT fixable** — skip agent, autorevert if enabled |
| `unknown` | Cannot determine from summary | **NOT fixable** (safe) — skip agent, autorevert if enabled |

The categorizer gate runs on **every** attempt (not cached), so if the
CI state changes between retries the stage re-evaluates accordingly.
Any LLM-call failure in the categorizer itself degrades to `unknown`
(safe default).

### Fixable failures (`test_failure`, `type_error`, `lint_error`, `build_error`)

- The CI-fix stage invokes `run_ci_fix_agent` on the ticket's workspace
  clone, passing it a summary of the failing checks and file-level
  annotations.
- On success the ticket branch is force-pushed (the ticket goes back to
  `IMPLEMENT_COMPLETE` for the next poll to re-verify both gates).
- On failure (after exhausting `MILL_CI_FIX_MAX_ATTEMPTS`) the ticket
  escalates to `BLOCKED` (resumable) — no half-fixed state is ever
  pushed.

### Unfixable failures (`env_error`, `unknown`)

- `run_ci_fix_agent` is **never called** — the attempt counter is reset
  to 0 so a future resume starts fresh.
- By default (`MILL_CI_AUTOREVERT=true`), the PR branch is force-reverted
  to the target branch tip (e.g. `origin/main`) to clean up the broken
  code.  This prevents unfixable PRs from blocking the target branch for
  other tickets.
- If `MILL_CI_AUTOREVERT=false`, the stage transitions to `BLOCKED`
  immediately with no git operations — manual intervention is required.
- The autorevert is best-effort: if any git operation fails (fetch,
  reset, force-push), the failure is logged at warning level and the
  stage still transitions to `BLOCKED` with a note that manual cleanup
  may be needed.

| Variable | Default | Description |
|---|---|---|
| `MILL_CI_FIX_MAX_ATTEMPTS` | `2` | Max CI-fix attempts per ticket before escalating to BLOCKED. Each attempt is one LLM invocation. |
| `MILL_CI_AUTOREVERT` | `true` | When the categorizer deems a CI failure unfixable (`env_error`/`unknown`), force-revert the PR branch to the target branch tip. Set to `false` to only skip fix attempts without reverting. |

The ci-fix agent uses the same sandboxed shell + file tools as the
implement agent, scoped to the ticket's clone. It never pushes, opens
PRs, or interacts with the forge. The agent does **NOT** have web-search
access — it works only from the failing summary and local files.

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

The categorizer is a separate, lightweight agent (`agent_definitions/ci_categorizer.yaml`)
using `MILL_DEDUP_MODEL` — no sandbox tools, no file access, one inference
per attempt.  It never pushes, opens PRs, or interacts with the forge.

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
- [docs/agents.md](agents.md) — agent catalog
