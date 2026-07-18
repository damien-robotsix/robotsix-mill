# Survey cross-board routing and generalizability classification

Survey periodic pass classifies findings as **repo-specific** or **fleet-wide
convention candidates** and routes fleet-wide findings to the
`robotsix-standards` board in addition to the local repo board.

## Agent prompt changes

`agent_definitions/periodic/survey.yaml` gained a
`GENERALIZABILITY CLASSIFICATION AND CROSS-BOARD ROUTING` section describing
the classification criteria and the two-draft filing protocol:

| Finding type | Draft 0 target | Draft 1 target |
|---|---|---|
| Repo-specific | `""` (current repo) | — |
| Fleet-wide | `"robotsix-standards"` | `""` (current repo) |

The agent sets `draft_target_repo_ids[0]` and optionally
`draft_target_repo_ids[1]` to control routing per draft.

## Runner support

1. **`PeriodicAgentResult.draft_target_repo_ids`** — new list field on the
   Pydantic result model, parallel to `draft_titles`/`draft_bodies`/`gap_ids`.
2. **`_resolve_board_id(settings, repo_id)`** — resolves a registered repo ID
   to a board ID via the repos registry. Raises `ValueError` for unknown IDs.
3. **`_verify_prior_on_board(settings, board_id, source_label)`** — checks a
   target board for prior proposals with the same gap_id, preventing
   re-filing the same convention from different repos.
4. **Cross-board ticket creation** — `run_agent_pass` loop now resolves the
   target board for each draft, runs cross-board dedup, and creates the
   ticket on the correct board via `target_service.create(board_id=...)`.

## Memory ledger

The agent tracks filed standards proposals in a `## Standards Proposals`
section in its memory ledger, reviewing it on subsequent runs to avoid
re-analysing the same convention. The ledger is pruned to the last 10
entries.

## Code changes

- `agent_definitions/periodic/survey.yaml` — prompt additions
- `src/robotsix_mill/agents/periodic_base.py` — `draft_target_repo_ids` field
- `src/robotsix_mill/agents/surveying.py` — `MAX_GAPS = 2`
- `src/robotsix_mill/runners/pass_runner.py` — cross-board resolution, dedup,
  and ticket creation
