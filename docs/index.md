# robotsix-mill

A self-contained LLM-driven ticket solver with a SQLite-backed management plane, file-based workspaces, and an event-driven worker that delivers merge requests to GitHub and GitLab.

## Key sections

- **[Agent catalog](agents/index.md)** — Every agent, grouped by pipeline / periodic / sub-agent, with YAML definition and Python module paths.
- **[Agent YAML schema](agents/agent-yaml-schema.md)** — Field reference for `agent_definitions/*.yaml` files.
- **[Expert YAML schema](agent-definitions/expert-yaml-schema.md)** — Field reference for `expert_definitions/*.yaml` files.
- **[Configuration & Deployment](config/configuration.md)** — Configure the service via YAML, deploy via Docker, and set up GitHub App / PAT authentication. See also the **[Configuration Audit](config/config-audit.md)** for a complete inventory of every config value.
- **[Reusable-workflow callers](reusable-workflow-callers.md)** — Canonical `ci.yml` / `docs.yml` callers a member repo uses to consume mill's shared reusable workflows (correct cross-repo org + per-job permissions).
- **[Pipeline](agents/index.md)** — Understand the agent catalog, approval gate, dedup guard (and the advisory **[epic-decomposition pre-filing dedup](epic-dedup.md)**), and merge stage.
- **[Observability](langfuse/observability.md)** — per-repo Langfuse + deployed-log config the refine agent consults.
- **[Operations](cost-and-resilience.md)** — Monitor costs, manage notifications, track ticket provenance, and clean up workspaces.
- **[Attaching screenshots](screenshots.md)** — Attach an image to a ticket from the board so the refine agent has visual context.
- **[Agent communication research](agents/communication-research.md)** — Phase 1 survey of existing agent-to-agent communication approaches feeding the planned architecture decision.

## Links

- [Source repository](https://github.com/robotsix/mill)
- [README](https://github.com/robotsix/mill#readme)
