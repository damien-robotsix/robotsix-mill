Fix the deploy-time config-standard footprint gate
(`validate_config_standard_footprint`) that globbed every `*.yaml`/`*.yml`
in a repo and blocked any delivery whose tree carried ordinary yaml
(`config/default.yaml`, a root `docker-compose.yml`, `.pre-commit-config.yaml`,
`mkdocs.yml`, …) — which was virtually every repo, blocking tickets
fleet-wide. The gate now only flags a genuine stray `_standards/` copy, the
one artifact that is never legitimate.
