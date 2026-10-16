# Approval gates

Robotsix-mill has two human approval gates: one for the refined spec
(before implementation) and one for the merge decision (before the
merge stage takes over).

## Spec approval (after refine)

By default (`MILL_REQUIRE_APPROVAL=true`), the refine stage transitions
tickets to `human_issue_approval` instead of `ready`. The pipeline pauses
until a human approves, giving you a chance to review the refined spec
before the implement stage starts. Approve via:

- **Web board:** open the ticket and click the "Approve" button in the
  detail drawer.
- **CLI:** `robotsix-mill ticket approve <id>`
- **API:** `POST /tickets/{id}/approve`

To run fully autonomous (refine → implement with no pause), set
`MILL_REQUIRE_APPROVAL=false`.

## Auto-approve triage

When `MILL_REQUIRE_APPROVAL=true` and `MILL_AUTO_APPROVE_ENABLED=true`,
a cheap, conservative LLM check inspects the refined spec **after**
refinement. If the spec is precise, unambiguous, and free of genuine
design or architecture decisions — regardless of how many files are
touched or whether logic changes — the ticket skips the human gate
and transitions straight to `READY`. When the triage returns
`NEEDS_APPROVAL` (or on any error), the ticket proceeds to
`HUMAN_ISSUE_APPROVAL` as usual.

This gives operators a middle ground between approving every ticket
(toil) and disabling the gate entirely (risk). The triage is **biased
conservative**: when unsure whether a genuine design decision exists,
it defers to the human.

The model used for triage is controlled by `MILL_AUTO_APPROVE_MODEL`
(default: level 1). Only the refined spec text is
inspected — no git diff, no repo exploration.

Auto-approved tickets record `"auto-approved: <reason>"` in their
event trail so operators can audit which tickets were auto-approved
and why.

## Policy-addition classification

The survey and audit periodic agents repeatedly propose a new standing
CI workflow, a new fleet-wide standard, or a new required check
(complexity gates, secret scanners, OpenSSF Scorecard, coverage
standardization, Conventional Commits, …). Each such proposal is a
**workflow-policy change** — a genuine operator decision, never a
routine auto-approvable code-quality follow-up — so it lands in
`human_issue_approval` and costs a separate escalation panel. In bulk
these saturate the operator's escalation capacity.

To make the class tractable, the pass runner classifies each
survey/audit draft with `is_policy_addition()` (a conservative
title + body keyword match) and stamps matching drafts with the
free-form **`policy-addition`** label at creation time
(`src/robotsix_mill/agents/runners/pass_runner.py`). The label is pure
metadata — it never suppresses or re-routes a draft — so a false
positive is cheap and a false negative just preserves the per-ticket
behaviour.

The label exists so policy additions can be **batched into a single
review decision** instead of one panel each: the chat-drain agent
recognises `policy-addition`-labelled tickets and groups them into one
policy-review digest rather than opening a panel per ticket. Tagging is
the foundation; the digest-aggregation step (collapsing N labelled
additions into one approvable unit, plus the approval-fanout semantics
that releases the approved items back into the pipeline) is tracked
separately because it is a pipeline-state-machine change — approving a
single digest ticket today would send it straight to `ready` →
implement, so fanning an approved digest back out into individual work
tickets needs its own design.

## MR approval (before merge)

After the deliver stage opens a PR, the ticket enters
`implement_complete`. The merge stage polls this state, verifying two
gates — **CI is green** and **PR is mergeable** — before promoting to
`human_mr_approval` and notifying the human. Only when both gates pass
does the ticket wait for an explicit human go-ahead:

### Merge (merge via forge)

The human approves the merge by clicking **Merge**, which calls the
forge's merge API immediately — identical to clicking "Merge pull
request" on GitHub.

- **Web board:** click the green **Merge** button in the ticket-detail
  drawer.
- **API:** `POST /tickets/{id}/merge-now`

On success the ticket transitions directly to `done` and retrospect runs.
If the forge rejects the merge (branch protection, conflicts, etc.), the
endpoint returns 409 and the ticket remains in `human_mr_approval`.

The drawer performs a **live merge-status check** before rendering the
Merge button. When the ticket drawer opens it calls
`GET /tickets/{id}/merge-status`, which queries the forge for the PR's
mergeability and CI conclusion:

- **Conflicts** → button is disabled with "PR has conflicts — rebase
  needed"
- **Failing CI** → button is disabled with "CI checks are failing"
- **Pending CI** → button is disabled with "CI checks are still running"
- **Mergeable + green CI** → button is active and clickable
- **Transient forge error** → button stays active (optimistic; the
  `merge-now` endpoint handles the actual rejection)

The drawer also calls `GET /tickets/{id}/merge-reason` to display an
amber annotation explaining *why* auto-merge is ineligible when it is.

### Merge Info panel

When a ticket is in `implement_complete` or `human_mr_approval`, the
detail drawer displays a **Merge Info** block between the Merge button
and the cost line. It is fetched from `GET /tickets/{id}/merge-info`
and surfaces three things the human needs:

- **CI status** — green checkmark (passing), red X with failing check
  names (failure), yellow spinner (pending), or grey dash (unknown).
- **Mergeable** — green checkmark (no conflicts), red X (conflicts
  detected), or grey dash (still computing).
- **Files changed** — a compact file list sorted by total line changes,
  capped at 50 files. Each file shows added/deleted line counts and
  status (`added`, `modified`, `removed`, `renamed`).

The merge-info is fetched once when the drawer opens (no auto-refresh).
Each sub-field is individually resilient — a forge error in one does not
break the others.

## See also

- [index.md](../index.md) — documentation home
- [cli/usage.md](../cli/usage.md) — full CLI command reference
- [docs/config/configuration.md](../config/configuration.md) — full env-var reference
