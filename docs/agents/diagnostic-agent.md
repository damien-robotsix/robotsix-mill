# Diagnostic agent

The diagnostic agent is a **deterministic, no-LLM** daily pass that
iterates a pluggable registry of independent checks (error detection,
draft-count self-health, …) across the repositories it monitors. It is a
plain-Python orchestrator — no model, no memory ledger — so it never
consumes tokens; each check inspects data sources (run logs, Langfuse)
and may auto-file draft tickets that then flow through the normal
pipeline.

## Monitored repositories

The set of repos the agent inspects on every pass is controlled by a
single list-typed setting:

| Surface | Value |
|---|---|
| YAML path | `periodic.diagnostic.monitored_repo_ids` |
| Env var | `MILL_DIAGNOSTIC_MONITORED_REPO_IDS` |
| Default | empty → falls back to `periodic.diagnostic.target_repo_id` |

When the list is empty (the default), the agent monitors the single
`periodic.diagnostic.target_repo_id` board — preserving the original
single-repo behavior. `target_repo_id` also remains the board the agent
routes its own activity to.

The monitored set is **config-only** — adding or removing a repo never
requires a code change.

### Add a repo

Append its `repo_id` (as it appears in `config/repos.yaml`) to the list:

```yaml
# config/config.yaml
periodic:
  diagnostic:
    monitored_repo_ids:
      - robotsix-mill
      - robotsix-llmio
```

Or via the environment (JSON list):

```sh
export MILL_DIAGNOSTIC_MONITORED_REPO_IDS='["robotsix-mill", "robotsix-llmio"]'
```

### Remove a repo

Delete its entry from the list. With the list emptied entirely, the
agent reverts to the single-repo fallback (`target_repo_id`).

## Accessibility validation

Before running any checks, the agent validates each monitored repo
against `config/repos.yaml` (the synthetic *meta* board is also
accepted). A repo that is neither registered nor the meta board is
**logged with a WARNING and skipped** — it never crashes the pass, and
the remaining valid repos still run. If loading `config/repos.yaml`
fails outright, the agent logs the failure and falls back to attempting
all configured repos unvalidated rather than aborting.

This mirrors the fail-safe / log-and-swallow contract of the diagnostic
data layer: a data-source or config-load outage must never crash a pass.

## Logging

At the start of **every** run the agent emits an INFO record naming the
monitored set, e.g.:

```
Diagnostic pass starting (session=…): monitoring 2 repo(s): ['robotsix-mill', 'robotsix-llmio'], checks=1
```

Any skipped/inaccessible repos are reported in a separate WARNING
record.

## Recurring CI failures → prevention rules

The `recurring_ci_failure` check is **summary-only**: it reports how the
board's `CI_FAILURE` events distribute across semantic buckets
(`ruff-format`, `ruff-lint`, `mypy`, `pytest-failure`,
`pytest-collection/import`, `modules-yaml-unregistered`, `vulture`, `deptry`, `codeql`, `trivy`,
`flaky-network`, `unknown`) and how many the ci-fix agent later resolved.
It files no tickets. (It used to auto-file one `[diagnostic] recurring CI
failure: key=…` report per normalized key; the key was a hash of the
failing check *names* — in practice always `ci / tests` — so the same eight
keys were re-filed on every pass, 21 reports in three passes, each then
consuming refine/implement cycles. `diagnostic_ci_failure_threshold` is
kept for config compatibility but inert.)

Learning from those failures is the job of the separate
**`ci_prevention_rules`** periodic pass
(`agent_definitions/periodic/ci_prevention_rules.yaml`, daily, enabled per
repo by a `.robotsix-mill/periodic/ci_prevention_rules.yaml` presence
file):

1. The ci-fix stage emits every confirmed red CI as a `CI_FAILURE` event
   carrying a deterministic `bucket`, a one-line `root_cause` picked from
   the job log and a default `prevention_rule`
   (`stages/ci_failure_buckets.py`); when its agent turns CI green it emits
   a paired `CI_FIX_RESOLVED` event with the agent's own account of the fix.
2. The pass reads the board's last `ci_prevention_rules_max_events`
   failures, renders a per-bucket digest and asks a small-tier agent for at
   most `ci_prevention_max_rules` short imperative rules.
3. It rewrites the `## CI prevention rules (auto-maintained)` section at
   the **top** of the board's implement memory ledger
   (`<data_dir>/<board_id>/implement_memory.md`) in place — never
   appending, removing the section when there are no rules, and preserving
   every other byte of the ledger. The implement coordinator injects that
   ledger into its prompt on every pass, so the rules reach the agent that
   caused the failures.

Run it by hand with `robotsix-mill ci-prevention-rules` or
`POST /passes/ci_prevention_rules/run?repo_id=<id>`.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `MILL_DIAGNOSTIC_PERIODIC` | `false` | Enable the weekly diagnostic pass |
| `MILL_DIAGNOSTIC_INTERVAL_SECONDS` | `604800` | Seconds between automatic passes |
| `MILL_DIAGNOSTIC_TARGET_REPO_ID` | `robotsix-mill` | Board the agent routes activity to; single-repo fallback when the monitored list is empty |
| `MILL_DIAGNOSTIC_MONITORED_REPO_IDS` | `[]` | Repos monitored each pass (JSON list); empty → falls back to `target_repo_id` |

## See also

- [index.md](../index.md) — documentation home
- [agent catalog](index.md) — agent catalog
- [docs/trace-health.md](../langfuse/trace-health.md) — a sibling deterministic check
- [docs/config/configuration.md](../config/configuration.md) — full env-var reference
