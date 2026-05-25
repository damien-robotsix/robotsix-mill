# robotsix-mill

Self-contained, LLM-driven ticket solver. Tickets go in one end, merge
requests come out the other. **No forge dependency for orchestration**
and **no scheduler** — emit a ticket and an agent takes it in charge
immediately. The only time it touches GitHub/GitLab is the final
*deliver* step.

**Status:** Full pipeline runs end-to-end.

## Configuration

Settings are managed through a YAML pipeline (see
[docs/configuration.md](docs/configuration.md) for full details):

- **`config/mill.defaults.yaml`** — committed canonical defaults (the
  single source of truth for every configurable knob).
- **`config/mill.local.yaml`** — your per-developer overrides
  (gitignored, create it if you need custom settings).
- **`config/mill.production.yaml`** — deployment-specific overrides
  (gitignored, path set via `MILL_CONFIG_FILE` env var).
- **Environment variables** — any `MILL_*` variable overrides the
  YAML value (e.g. `MILL_MODEL=anthropic/claude-sonnet-4`).
- **`config/secrets.yaml`** — credentials (API keys, tokens).
  Template at `config/secrets.example.yaml`.
- **`config/repos.yaml`** — per-repo board & Langfuse project config.
  Template at `config/repos.example.yaml`.

The loading order is: YAML defaults → YAML local → YAML production →
environment variables (highest).

## Quickstart

### Docker (recommended)

```sh
cp config/secrets.example.yaml config/secrets.yaml       # set openrouter_api_key
cp config/repos.example.yaml config/repos.yaml           # edit: add your repo
docker compose up -d --build                             # defaults to MILL_REPO_ID=robotsix-mill;
                                                         # edit docker-compose.yml or pass -e MILL_REPO_ID=... to override
```

Open `http://localhost:8077` — the ticket board is the primary interface.

The server requires a repo identity at startup. The compose file defaults
to `MILL_REPO_ID=robotsix-mill`; override via `-e MILL_REPO_ID=...` or by
editing `docker-compose.yml`. When running outside Docker, pass
`--repo-id <id>` or export `MILL_REPO_ID`. See
[docs/configuration.md#repos-registry](docs/configuration.md#repos-registry).

```sh
docker compose exec mill robotsix-mill ticket new --title "Add X" --description-file -
docker compose exec mill robotsix-mill ticket list
docker compose exec mill robotsix-mill ticket show <id>
docker compose exec mill robotsix-mill ticket approve <id>
docker compose exec mill robotsix-mill audit
docker compose exec mill robotsix-mill trace-health
```

### Local dev (no Docker)

```sh
cp config/secrets.example.yaml config/secrets.yaml       # set openrouter_api_key
cp config/repos.example.yaml config/repos.yaml           # edit: add your repo
make install                    # venv + editable install
MILL_REPO_ID=my-repo make dev   # hot-reload on http://127.0.0.1:8077
```

```sh
.venv/bin/robotsix-mill ticket new --title "Add X" --description-file -
.venv/bin/robotsix-mill ticket list
.venv/bin/robotsix-mill ticket show <id>
.venv/bin/robotsix-mill ticket approve <id>
.venv/bin/robotsix-mill audit
.venv/bin/robotsix-mill trace-health
make test
```

Running the pipeline needs Docker (agents run in disposable containers);
`make test` works without it.

## Documentation

- [docs/configuration.md](docs/configuration.md) — Complete configuration reference (YAML schema, loading order, secrets)
- [docs/deployment.md](docs/deployment.md) — Continuous deployment via GitHub Actions + Watchtower
- [docs/docker-architecture.md](docs/docker-architecture.md) — Container topology & conceptual architecture
- [docs/github-app.md](docs/github-app.md) — Delivery identity setup (PAT or GitHub App bot)
- [docs/security.md](docs/security.md) — Security model
- [docs/agents.md](docs/agents.md) — Full agent catalog
- [docs/agent-yaml-schema.md](docs/agent-yaml-schema.md) — Field reference for `agent_definitions/*.yaml` files
- [docs/approval-gate.md](docs/approval-gate.md) — Human approval gate after refine
- [docs/dedup-guard.md](docs/dedup-guard.md) — Pre-refine duplicate / already-done check
- [docs/merge-stage.md](docs/merge-stage.md) — Auto-rebase of stale PRs + auto-fix of failing CI
- [docs/audit-agent.md](docs/audit-agent.md) — Meta-audit agent for quality/security coverage gaps
- [docs/blocked-ticket-recovery.md](docs/blocked-ticket-recovery.md) — Recovering from BLOCKED tickets
- [docs/retrospect-memory.md](docs/retrospect-memory.md) — Retrospect agent's Markdown memory ledger
- [docs/trace-health.md](docs/trace-health.md) — Deterministic check for unsessioned Langfuse traces
- [docs/cost-and-resilience.md](docs/cost-and-resilience.md) — Per-ticket cost tracking & cost controls
- [docs/notifications.md](docs/notifications.md) — ntfy.sh push notifications for human-attention states
- [docs/ticket-provenance.md](docs/ticket-provenance.md) — How `source` tracks which actor created each ticket
- [docs/workspace-cleanup.md](docs/workspace-cleanup.md) — Automatic clone pruning on ticket close

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE) for details.
