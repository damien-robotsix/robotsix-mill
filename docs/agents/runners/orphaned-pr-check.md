# Orphaned-PR check

Operator-facing reference for the periodic orphaned-PR maintenance pass.

The mill runs background maintenance passes on a schedule, one per
registered repo. Each pass is config-gated and respects dry-run mode.

## Overview

Detects open PRs on managed repos with no active mill ticket driving them
and either auto-closes obsolete ones or files a tracking ticket.

**Enabling.** Opt-in — default `false`. Set
`orphaned_pr_check.enabled` to `true` in `config/config.yaml`.

**Dry-run.** Default `true` (safe). Logs all actions but makes no forge
mutations. Set `orphaned_pr_check.dry_run` to `false` for real closes and
ticket filing. Every log line carries `dry_run=true/false`.

**Age guard.** A PR whose tracking ticket was created within
`orphaned_pr_check.min_age_hours` (default **4 hours**) is silently
skipped to avoid racing the deliver stage.

**Action cap.** `orphaned_pr_check.max_actions_per_pass` (default 5) caps
combined close + file-ticket actions per pass. Findings beyond the cap are
deferred to the next pass; the remaining count is logged at `INFO`.
Per-type sub-caps `orphaned_pr_check.max_closes_per_pass` and
`orphaned_pr_check.max_files_per_pass` enforce independently alongside the
combined cap.

**Author & branch guard.** Only branches matching `settings.branch_prefix`
are evaluated. **Human-authored PRs are never touched.** The author check
uses `orphaned_pr_check.bot_logins` (explicit bot login list) or falls
back to `get_authenticated_user_login()`; when both resolve empty the
author check is skipped with a `WARNING` (fail-open) while the
branch-prefix filter remains active.

## Classification & actions

Each orphaned mill PR lands in one of two buckets:

- **Auto-close** — empty diff, ticket DONE/CLOSED (merged-equivalent or
  conflicting), no-ticket empty diff, no-ticket conflicting,
  errored + empty diff, errored + conflicting. A forge comment explains
  the reason; the PR is then closed.
- **File tracking ticket** — non-empty, non-conflicting diff with no
  active ticket (`NO_TICKET`) or an errored ticket (`TICKET_ERRORED`).
  Files a ticket titled `Track orphaned PR: <repo_id>/<branch>`,
  routed via `SourceKind.ORPHANED_PR_CHECK`.

## Foreign (non-board) PR tracking

By default the pass only considers **board-authored** PRs (branch starts
with `settings.branch_prefix`, e.g. `mill/`). Set
`orphaned_pr_check.track_foreign_prs` to `true` (default `false`,
opt-in) to also file a tracking ticket for **foreign** PRs — open PRs
whose head branch does *not* start with the branch prefix (e.g.
Dependabot `dependabot/*` bumps or human `feature/*` branches).

- Foreign PRs are **never closed** — they are external, so the board
  decides what to do. The pass only files a tracking ticket.
- The tracking ticket uses a deterministic, idempotent title
  `Track external PR: <repo_id>#<pr_number>` (falling back to
  `Track external PR: <repo_id>/<branch>` when the forge dict lacks a
  PR number), routed via `SourceKind.ORPHANED_PR_CHECK`. A second pass
  is a no-op — the title is de-duped against non-terminal
  `ORPHANED_PR_CHECK` tickets exactly like the board-PR tracking
  tickets.
- The ticket body carries the PR number, URL, author login, branch and
  title, plus a one-line instruction to review-and-merge or close.
- Foreign file-ticket actions count against the same
  `max_files_per_pass` and combined `max_actions_per_pass` caps as the
  board-PR actions; the mill-PR phase runs first, so its actions consume
  the shared budget before foreign PRs are considered. Deferred foreign
  PRs are logged and re-considered next pass.
- Requires `forge.list_open_prs()` to return per-PR metadata (branch,
  author_login, number, url, title). GitHub and GitLab both provide it;
  forges that fall back to `list_open_pr_branches` (no metadata) do not
  process foreign PRs.

## Idempotency

A second pass is a no-op: PRs already closed on the forge are skipped
(forge `pr_status` check); already-filed tracking tickets are detected by
the deterministic title `Track orphaned PR: <repo_id>/<branch>` (board
PRs) or `Track external PR: <repo_id>#<pr_number>` (foreign PRs), queried
as `SourceKind.ORPHANED_PR_CHECK` tickets not in a terminal state.

## Logging

Every action (or would-be action under dry-run) is logged at `INFO` with
structured fields:

```
repo=<repo_id> branch=<branch> ticket_state=<state|NOT_FOUND>
action=<CLOSE|FILE_TICKET|DEDUP_SKIP> classification=<reason>
dry_run=<true|false>
```

## Config example

From `config/config.example.yaml`:

```yaml
periodic:
  orphaned_pr_check:
    enabled: false                  # opt-in: enable periodic orphaned-PR check passes
    interval_seconds: 86400         # seconds between passes (min 3600 enforced in worker)
    min_age_hours: 4                # minimum ticket age before PR is considered orphaned
    max_actions_per_pass: 5         # max combined close+file actions per pass
    dry_run: true                   # log intent only, no forge mutations
    track_foreign_prs: false        # also file tracking tickets for non-mill PRs (never closes them)
```
