# robotsix-mill

A self-contained LLM-driven ticket solver with a SQLite-backed management plane, file-based workspaces, and an event-driven worker that delivers merge requests to GitHub and GitLab.

## Key sections

- **[Agent catalog](agents.md)** — Every agent, grouped by pipeline / periodic / sub-agent, with YAML definition and Python module paths.
- **[Agent YAML schema](agent-yaml-schema.md)** — Field reference for `agent_definitions/*.yaml` files.
- **[Configuration & Deployment](configuration.md)** — Configure the service, deploy via Docker, and set up GitHub App / PAT authentication. See also the **[Configuration Audit](config-audit.md)** for a complete inventory of every config value.
- **[Pipeline](agents.md)** — Understand the agent catalog, approval gate, dedup guard, and merge stage.
- **[Operations](cost-and-resilience.md)** — Monitor costs, manage notifications, track ticket provenance, and clean up workspaces.

## Links

- [Source repository](https://github.com/robotsix/mill)
- [README](https://github.com/robotsix/mill#readme)
