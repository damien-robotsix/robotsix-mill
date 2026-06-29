# CLAUDE.md — operator-facing config & behaviour reference

For agent conventions, Git/CI rules, testing, board-UI, module taxonomy,
and forge-adapter patterns see [AGENT.md](AGENT.md).  This file documents
operator-facing configuration knobs and runtime behaviours.

---

## Periodic Maintenance

The mill runs background maintenance passes on a schedule, one per
registered repo.  Each pass is config-gated and respects dry-run mode.

### Orphaned-PR check

Detects open PRs on managed repos that have no active mill ticket driving
them and either auto-closes obsolete ones or files a tracking ticket.

**Enabling.**  Opt-in — default `false`.  Set `orphaned_pr_check_periodic`
(env `MILL_ORPHANED_PR_CHECK_PERIODIC`) or YAML `orphaned_pr_check.enabled`
to `true`.

**Dry-run.**  Default `true` (safe).  All actions are logged but no forge
mutations occur.  Set `orphaned_pr_dry_run` (YAML
`orphaned_pr_check.dry_run`) to `false` for real closes and ticket filing.
Every log line carries `dry_run=true/false`.

**Age guard.**  A PR whose tracking ticket was created within
`orphaned_pr_min_age_hours` (YAML `orphaned_pr_check.min_age_hours`,
default **4 hours**) is silently skipped to avoid racing the deliver stage.

**Action cap.**  `orphaned_pr_max_actions_per_pass` (YAML
`orphaned_pr_check.max_actions_per_pass`, default 5) caps combined
close + file-ticket actions per pass.  Findings beyond the cap are
deferred to the next pass; the remaining count is logged at `INFO`.
The sibling dep (`20260628T233938Z`) adds per-type sub-caps
`orphaned_pr_max_closes_per_pass` and `orphaned_pr_max_files_per_pass`
that enforce independently while the combined cap also applies
(not yet in the settings file).

**Author & branch guard.**  Only branches matching `settings.branch_prefix`
are evaluated.  **Human-authored PRs are never touched.**  The author
check uses `orphaned_pr_bot_logins` (explicit bot login list) or falls
back to `get_authenticated_user_login()`; when both resolve empty the
author check is skipped with a `WARNING` (fail-open) while the
branch-prefix filter remains active.  `orphaned_pr_bot_logins` is added
by the sibling dep and not yet in the settings file.

**Classification & actions.**  Each orphaned mill PR lands in one of
two buckets:

- **Auto-close** — empty diff, ticket DONE/CLOSED (merged-equivalent or
  conflicting), no-ticket empty diff, no-ticket conflicting,
  errored + empty diff, errored + conflicting.  A forge comment explains
  the reason; the PR is then closed.
- **File tracking ticket** — non-empty, non-conflicting diff with no
  active ticket (`NO_TICKET`) or an errored ticket (`TICKET_ERRORED`).
  Files a ticket titled `Track orphaned PR: <repo_id>/<branch>`,
  routed via `SourceKind.ORPHANED_PR_CHECK`.

**Idempotency.**  A second pass is a no-op: PRs already closed on the
forge are skipped (forge `pr_status` check); already-filed tracking
tickets are detected by the deterministic title
`Track orphaned PR: <repo_id>/<branch>`, queried as
`SourceKind.ORPHANED_PR_CHECK` tickets not in a terminal state.

**Logging.**  Every action (or would-be action under dry-run) is
logged at `INFO` with structured fields:

```
repo=<repo_id> branch=<branch> ticket_state=<state|NOT_FOUND>
action=<CLOSE|FILE_TICKET|DEDUP_SKIP> classification=<reason>
dry_run=<true|false>
```

**YAML config example** (from `config/mill.defaults.yaml`):

```yaml
periodic:
  orphaned_pr_check:
    enabled: false                  # opt-in: enable periodic orphaned-PR check passes
    interval_seconds: 86400         # seconds between passes (min 3600 enforced in worker)
    min_age_hours: 4                # minimum ticket age before PR is considered orphaned
    max_actions_per_pass: 5         # max combined close+file actions per pass
    dry_run: true                   # log intent only, no forge mutations
```
