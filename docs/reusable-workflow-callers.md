# Reusable-workflow callers for member repos

The shared reusable GitHub Actions workflows (`python-ci.yml` and
`python-docs.yml`) live in the `damien-robotsix/robotsix-github-workflows`
repository. A member repo wires up CI and docs by adding small *caller*
workflows that reference them across repos.

Hand-authoring these callers recurrently goes wrong in two ways, each
producing a `startup_failure` ("workflow file issue") that turns the
repo's `main` red and masks every real gate behind it:

1. **Wrong org.** Callers reference
   `uses: robotsix/robotsix-github-workflows/.github/workflows/...@main`.
   The org is `damien-robotsix`, **not** `robotsix`;
   `robotsix/robotsix-github-workflows` does not resolve.
2. **Missing permissions grant.** The reusable `python-ci.yml` `tests`
   job declares `permissions: security-events: write` (and needs
   `contents: read` for private checkout). A calling job that grants no
   `permissions:` block cannot provide what the reusable workflow
   requests → `startup_failure`.

> [!note]
> mill's OWN callers (`.github/workflows/ci.yml`, `docs.yml`) also use
> the cross-repo form below, pinned to a concrete commit SHA. Every repo
> — including mill itself — must use the CROSS-REPO form; there is no
> local `./...` form for these workflows.

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
    uses: damien-robotsix/robotsix-github-workflows/.github/workflows/python-ci.yml@main
```

## `.github/workflows/dependabot-auto-merge.yml`

```yaml
name: Dependabot auto-merge

on:
  pull_request:

jobs:
  auto-merge:
    permissions:
      contents: write
      pull-requests: write
    uses: damien-robotsix/robotsix-github-workflows/.github/workflows/dependabot-auto-merge.yml@9ea2955d
```

The caller delegates to the shared reusable workflow, which gates on
`github.actor` matching `dependabot[bot]` or `renovate[bot]`.

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
    uses: damien-robotsix/robotsix-github-workflows/.github/workflows/python-docs.yml@main
```
