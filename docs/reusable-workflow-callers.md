# Reusable-workflow callers for member repos

robotsix-mill hosts shared reusable GitHub Actions workflows
(`python-ci.yml` and `python-docs.yml`). A member repo wires up CI and
docs by adding small *caller* workflows that reference them across repos.

Hand-authoring these callers recurrently goes wrong in two ways, each
producing a `startup_failure` ("workflow file issue") that turns the
repo's `main` red and masks every real gate behind it:

1. **Wrong org.** Callers reference
   `uses: robotsix/robotsix-mill/.github/workflows/...@main`. The org is
   `damien-robotsix`, **not** `robotsix`; `robotsix/robotsix-mill` does
   not resolve.
2. **Missing permissions grant.** The reusable `python-ci.yml` `tests`
   job declares `permissions: security-events: write` (and needs
   `contents: read` for private checkout). A calling job that grants no
   `permissions:` block cannot provide what the reusable workflow
   requests → `startup_failure`.

> [!note]
> mill's OWN callers (`.github/workflows/ci.yml`, `docs.yml`) use the
> LOCAL path `./.github/workflows/python-ci.yml` because mill hosts the
> reusable workflows. A member repo must use the CROSS-REPO form below —
> do **not** copy the local `./...` form.

The repo scaffold (`run_repo_scaffold`) emits these callers for python
repos automatically, so generated callers are correct by construction.
For an existing repo, copy the snippets below.

## `.github/workflows/ci.yml`

```yaml
name: CI

on: [pull_request, push]

permissions:
  contents: read

jobs:
  ci:
    permissions:
      contents: read
      security-events: write
    uses: damien-robotsix/robotsix-mill/.github/workflows/python-ci.yml@main
```

## `.github/workflows/docs.yml`

```yaml
name: Docs

on:
  push:
    branches: [main]
  workflow_dispatch:

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

permissions:
  contents: read

jobs:
  deploy:
    permissions:
      contents: write
    uses: damien-robotsix/robotsix-mill/.github/workflows/python-docs.yml@main
```
