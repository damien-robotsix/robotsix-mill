# Testing the GitLab Integration

This guide covers how to run the existing hermetic GitLab tests and how
to smoke-test against a real GitLab instance (self-hosted or gitlab.com).

## Prerequisites

- A GitLab instance with API access (gitlab.com or self-hosted).
- A project on that instance.
- A **Personal Access Token (PAT)** with the `api` scope.
  Create one at **Settings → Access Tokens** in your GitLab project or
  group.

## Configuration

Set these environment variables (or add them to `config/mill.local.yaml`):

```bash
export FORGE_KIND=gitlab
export FORGE_REMOTE_URL="https://gitlab.com/your-namespace/your-project.git"
export FORGE_TOKEN="glpat-xxxxxxxxxxxxxxxxxxxx"
```

For a **self-hosted** GitLab instance, also set the API base URL:

```bash
export FORGE_GITLAB_API_URL="https://gitlab.mycompany.com/api/v4"
```

The default is `https://gitlab.com/api/v4`.

## Running the hermetic tests (no real HTTP)

All GitLab tests in this repository are **hermetic** — they use mocked
`httpx.Client` responses and never touch a real network. The
`_no_real_http` autouse fixture in `tests/conftest.py` blocks any
accidental outbound HTTP.

### Unit tests (forge HTTP seams)

```bash
pytest tests/forge/test_gitlab.py -v
```

This exercises every GitLab API seam method (`_create_mr`, `_find_mr`,
`_get_latest_pipeline`, `_merge_mr`, etc.) in isolation against mocked
responses.  No configuration is needed — the tests set up their own
`FORGE_KIND`, `FORGE_TOKEN`, and `FORGE_REMOTE_URL` inline.

### Workflow integration test

```bash
pytest tests/forge/test_gitlab_workflow.py -v
```

A single test chains `open_merge_request` → `pr_status` →
`check_status` → `pr_review_status` → `merge_pr` against one consistent
mocked GitLab API surface.  This validates the full MR lifecycle without
real HTTP.

### Stage tests (GitLab variants)

```bash
# Merge stage — GitLab variants
pytest tests/stages/test_merge.py -k gitlab -v

# Deliver stage — GitLab variants
pytest tests/stages/test_deliver.py -k gitlab -v

# CI-fix stage — GitLab variants
pytest tests/stages/test_ci_fix.py -k gitlab -v

# All GitLab stage tests at once
pytest tests/stages/ -k gitlab -v
```

These tests use `monkeypatch` to replace `GitLabForge` methods (e.g.
`pr_status`, `check_status`, `open_merge_request`) and verify the stage
state-machine transitions when GitLab is the forge backend.  The return
shapes are identical to the GitHub variants — the `Forge` ABC contract
guarantees this.

## Testing against a real GitLab instance

The `_no_real_http` autouse fixture **blocks all real outbound HTTP**
during `pytest` runs.  To test against a real GitLab instance you must
use a standalone script (not pytest).

Below is a short smoke-test script (~20 lines) that constructs a
`GitLabForge`, opens a real MR, and prints the result:

```python
"""quick-gitlab-smoke.py — open a real MR on GitLab.  NOT a pytest test."""
import os
from robotsix_mill.config import Settings, Secrets, _reset_secrets
import robotsix_mill.config as _cfg
from robotsix_mill.forge.gitlab import GitLabForge

# Point Secrets at your real token.
_reset_secrets()
_cfg._secrets = Secrets(forge_token=os.environ["FORGE_TOKEN"])

s = Settings(
    data_dir="/tmp/mill-smoke",
    FORGE_KIND="gitlab",
    FORGE_REMOTE_URL=os.environ["FORGE_REMOTE_URL"],
    FORGE_TOKEN=os.environ["FORGE_TOKEN"],
    # Self-hosted only:
    # FORGE_GITLAB_API_URL=os.environ.get("FORGE_GITLAB_API_URL",
    #                                     "https://gitlab.com/api/v4"),
)

forge = GitLabForge(s)
url = forge.open_merge_request(
    source_branch="main",          # ← change to a real branch
    title="[smoke test] please ignore",
    body="This MR was opened by the testing-gitlab.md smoke script.",
)
print(f"MR opened: {url}")
```

Run it:

```bash
FORGE_TOKEN="glpat-..." \
FORGE_REMOTE_URL="https://gitlab.com/your-ns/your-project.git" \
python quick-gitlab-smoke.py
```

The script will open a real MR on your GitLab project.  Close or delete
it afterwards.

## Self-hosted GitLab

The `gitlab_api_url` setting defaults to `https://gitlab.com/api/v4`.
For self-hosted instances, set `FORGE_GITLAB_API_URL` to your instance's
API base (e.g. `https://gitlab.mycompany.com/api/v4`).  The `GitLabForge`
adapter reads this from `Settings.gitlab_api_url` at request time, so
you can set it via env var or `config/mill.local.yaml`.
