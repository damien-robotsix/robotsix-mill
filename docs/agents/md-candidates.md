# AGENT.md candidates

The retrospect agent periodically audits tickets and proposes **new rules
for `AGENT.md`** based on observed patterns and conventions in the codebase.
Each proposal is recorded as a `Candidate` row with status `PENDING` and
surfaces in the board's **AGENT.md** panel, where an operator reviews it and
either validates (which files an audited-repo draft ticket proposing the edit)
or rejects it (which dismisses the proposal without action). The point is
human review: an agent that wants to add a new rule to `AGENT.md` — but
wants operator sign-off before filing a ticket — proposes a candidate and
waits.

## The data model

A `Candidate` (`src/robotsix_mill/agents/candidates.py`) carries the
fields an operator sees in the panel:

- `candidate_id` — a stable identifier for the proposal (8 hex characters,
  based on the proposed rule and section).
- `section` — the section of `AGENT.md` it targets (e.g. `"## Project
  layout"`, `"## Testing conventions"`).
- `rule` — the actual proposed text for the rule.
- `rationale` — free-text explanation from the retrospect agent (e.g.,
  "observed across tickets `aaa`, `bbb`").
- `proposed_at` — when the proposal was first filed (ISO 8601 timestamp).
- `source_ticket` — the ticket ID that triggered the proposal.
- `status` — lifecycle state (`"pending"`, `"validated"`, or `"rejected"`).
- `filed_ticket` — the draft ticket ID filed by validate, or `None` for
  pending and rejected entries.

### Candidate lifecycle

```
PENDING ──validate──> VALIDATED (files draft ticket)
   │
   └──reject──> REJECTED
```

A candidate starts `PENDING`. Validating stamps it `VALIDATED` and files
a draft ticket on the audited repo proposing the `AGENT.md` edit. Rejecting
moves it to `REJECTED` and runs nothing. Once acted on, a candidate stays
in the file as an audit trail (visibility is controlled by the UI filter).

## The AGENT.md panel (board UI)

A `📋 AGENT.md` button sits in the board toolbar header. Clicking it
(`openCandidates()`) opens the right-hand drawer; it is mutually exclusive
with the Runs and Cost panels (opening one closes the others).

The panel shows **pending** candidates for the **currently selected repo**.
When the repo selector is on "all repos", the panel aggregates pending
candidates from every repo and displays each with a repo badge showing
its source repo. This allows operators to review and validate/reject
candidates across multiple repos without switching between them.

The panel hides the button when `repo_id === "meta"` (the synthetic meta
board has no `AGENT_CANDIDATES.md` file).

## Candidate display and interaction

Each pending candidate renders as a card showing:

- The **repo badge** (when in "all repos" mode), identifying the source repo.
- The **section** it targets (e.g., `"## Project layout"`).
- The **rule** — the proposed text for `AGENT.md`.
- The **rationale** — why the agent proposes this rule.
- The **source ticket** — the ticket ID that triggered the proposal.
- **Validate** and **Reject** action buttons.

Clicking **Validate** files a draft ticket on the owning repo with a title
like `"Propose AGENT.md rule: <rule summary>"` and a body that includes the
rule, rationale, and source ticket. The operator can then review, edit, and
approve the draft as a normal ticket.

Clicking **Reject** dismisses the candidate without filing a ticket.

The panel auto-refreshes about once a second while open, so decisions made
elsewhere disappear from the list on the next tick.

## Single-repo vs. multi-repo mode

### Single-repo mode (`repo_id` is a specific repo)

The panel fetches `GET /candidates?repo_id=<specific>` and shows only that
repo's pending candidates. Validate/reject operations target that repo's
`AGENT_CANDIDATES.md` file.

### Multi-repo mode (`repo_id` is `"all"` or empty)

The panel fetches `GET /candidates?repo_id=all` and aggregates pending
candidates from every repo, each tagged with its owning `repo_id`. When
a user validates or rejects a candidate, the operation targets that
candidate's source repo (read from the `data-repo` attribute on the card).

This allows efficient batch review of cross-repo candidates without
switching repos.

## Validate / reject workflow

Validating a candidate (`POST /candidates/{id}/validate?repo_id=<repo>`)
stamps it `PENDING → VALIDATED`, files a draft ticket on the owning repo,
and updates the file. Rejecting
(`POST /candidates/{id}/reject?repo_id=<repo>`) stamps it `PENDING →
REJECTED` and files no ticket.

### Idempotency / re-action guard

Only `PENDING` candidates can be validated or rejected. Validating or
rejecting an already-acted-on candidate returns HTTP `409`.

## API endpoint reference

Routes live in
`src/robotsix_mill/runtime/routes/_candidates.py`. All return the full
`Candidate` JSON (the list route returns an array); an unknown id yields
`404`, and validating/rejecting a non-`PENDING` candidate yields `409`.

- `GET /candidates` — list candidates, newest first. Optional
  `?repo_id=` (e.g., `"all"`, a specific repo key, or empty) and
  `?include_acted=` filters. When `repo_id` is omitted, `"all"`, or a
  specific repo, results from that repo or all repos are returned. By
  default only `PENDING` candidates are returned; pass `include_acted=true`
  to include validated and rejected entries as audit trail.
- `GET /candidates/{candidate_id}` — a single candidate by id. Optional
  `?repo_id=` disambiguates the board. `404` on miss.
- `POST /candidates/{candidate_id}/validate` — validate and file a draft
  ticket. Required `?repo_id=` parameter. `404` on unknown id, `409` if
  not `PENDING`.
- `POST /candidates/{candidate_id}/reject` — reject (no ticket filed).
  Required `?repo_id=` parameter. `404` on unknown id, `409` if not
  `PENDING`.

## Common scenarios / troubleshooting

- **The button is hidden.** It only appears when `repo_id !== "meta"`. The
  synthetic meta board has no `AGENT_CANDIDATES.md` file.
- **The panel is empty.** No pending candidates exist for the selected repo
  (or any repo if in "all repos" mode). Check the board's recent activity
  — retrospect files new candidates as it runs.
- **Validate / Reject returns `409`.** The candidate was already acted on
  — most likely by another operator or browser tab. Refresh the panel (it
  auto-refreshes about once a second) to pick up the new status.
- **Validate filed a draft, but I want to edit it before approving.** Open
  the board and click on the draft ticket. The ticket body contains the
  proposed rule, rationale, and source — edit it as needed, then approve
  to merge the change.

## See also

- [index.md](../index.md) — documentation home
- [docs/board-operations.md](../board-operations.md) — board UI and automated column
  transitions
- [agent catalog](index.md) — agent catalog (includes retrospect)
