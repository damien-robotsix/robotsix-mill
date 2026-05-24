# robotsix-mill

Self-contained, LLM-driven ticket solver. Tickets go in one end, merge
requests come out the other. **No forge dependency for orchestration**
and **no scheduler** — emit a ticket and an agent takes it in charge
immediately. The only time it touches GitHub/GitLab is the final
*deliver* step.

**Status:** Full pipeline runs end-to-end.

## Quickstart

### Docker (recommended)

```sh
cp secrets.env.example secrets.env      # set OPENROUTER_API_KEY
docker compose up -d --build
```

Open `http://localhost:8077` — the ticket board is the primary interface.

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
cp secrets.env.example secrets.env        # set OPENROUTER_API_KEY
make install                # venv + editable install
make dev                    # hot-reload on http://127.0.0.1:8077
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

- [docs/configuration.md](docs/configuration.md) — Complete env-var reference
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
