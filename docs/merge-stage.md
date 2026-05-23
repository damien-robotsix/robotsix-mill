# Merge stage: auto-rebase & CI-fix

The merge stage monitors open PRs and automatically handles two common
failure modes: merge conflicts from a stale branch, and failing remote
CI checks.

## Auto-rebase of stale PRs

When a PR sits `in_review` while other PRs merge onto the target branch,
it may become stale and develop merge conflicts. Rather than stranding
such PRs, the merge stage automatically invokes a **rebase agent**
(`agents/rebasing.py`) that resolves conflicts using the LLM.

- The forge's PR status includes a `mergeable` flag.
- If a PR is open and **mergeable**, the existing no-op (re-poll) path
  is preserved exactly.
- If a PR is open and **conflicting**, the merge stage invokes
  `run_rebase_agent` on the ticket's workspace clone.
- On success the ticket branch is force-pushed (the ticket stays
  `in_review` for the next poll to observe the now-mergeable PR).
- On failure the ticket escalates to `BLOCKED` (resumable) — no
  half-rebased state is ever pushed.

| Variable | Default | Description |
|---|---|---|
| `MILL_REBASE_MAX_ATTEMPTS` | `5` | Max rebase attempts per ticket before escalating to BLOCKED. Each attempt is one LLM invocation. |

The rebase agent uses the same sandboxed shell + file tools as the
implement agent, scoped to the ticket's clone. It never pushes, opens
PRs, or interacts with the forge.

## Auto-fix of failing remote CI

When a PR sits `in_review` with mergeable code but **failing** remote CI
checks (GitHub Actions), the merge stage transitions the ticket to
`fixing_ci` and invokes a **ci-fix agent** (`agents/ci_fixing.py`) that
analyses the failing check-run output and applies minimal fixes.

- The forge adapter fetches check-run status (and falls back to the
  combined commit-statuses API for older repos).
- If a mergeable PR has failing CI, the merge stage transitions to
  `FIXING_CI` instead of staying `IN_REVIEW`.
- The CI-fix stage invokes `run_ci_fix_agent` on the ticket's workspace
  clone, passing it a summary of the failing checks and file-level
  annotations.
- On success the ticket branch is force-pushed (the ticket goes back to
  `IN_REVIEW` for the next poll to observe the now-green CI).
- On failure the ticket escalates to `BLOCKED` (resumable) — no
  half-fixed state is ever pushed.

| Variable | Default | Description |
|---|---|---|
| `MILL_CI_FIX_MAX_ATTEMPTS` | `2` | Max CI-fix attempts per ticket before escalating to BLOCKED. Each attempt is one LLM invocation. |

The ci-fix agent uses the same sandboxed shell + file tools as the
implement agent, scoped to the ticket's clone. It never pushes, opens
PRs, or interacts with the forge. The agent does **NOT** have web-search
access — it works only from the failing summary and local files.

## See also

- [index.md](index.md) — documentation home
- [docs/configuration.md](configuration.md) — full env-var reference
- [docs/agents.md](agents.md) — agent catalog
