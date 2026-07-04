# Delivery identity: GitHub App bot (recommended) or PAT

The `deliver` stage pushes the ticket branch and opens a Pull Request.
Two ways to authenticate, set via `FORGE_AUTH` in `config/config.json`
(or as an environment variable):

| `FORGE_AUTH` | Identity on the PR/commits | Setup |
|---|---|---|
| `token` | **you** (the PAT owner) | 1 line |
| `app` (recommended) | a **bot**: `<app-slug>[bot]` | one-time App creation |

The App path mints a short-lived **installation access token** at
deliver time (JWT signed with the App private key → installation
token). No GitHub Actions needed — mill does it in-process. Same bot
identity as robotsix-project.

---

## Common forge settings

```yaml
# config/config.json
forge:
  kind: github
  remote_url: https://github.com/<owner>/<repo>.git
  target_branch: main
```

## Option A — PAT (quick, for testing)

```yaml
# config/config.json
secrets:
  forge_token: <token>
```
With `forge.auth_mode: token` (the default) in your settings.

Fine-grained PAT scoped to the repo with **Contents: Read/write** +
**Pull requests: Read/write** (or a classic PAT with `repo`). The PR is
authored by you.

## Option B — GitHub App bot (recommended)

A one-time setup; afterwards every delivery is authored by the bot.

### 1. Create the App

GitHub → **Settings → Developer settings → GitHub Apps → New GitHub App**.

| Field | Value |
|---|---|
| **Name** | e.g. `robotsix-mill-<you>` (globally unique) |
| **Homepage URL** | anything (your repo URL) |
| **Webhook → Active** | **uncheck** (mill doesn't use webhooks) |
| **Where can this App be installed?** | Only on this account |

**Repository permissions** (least privilege — covers the full
default feature set: `deliver` + merge gate + CI monitor):

| Permission | Access | Why |
|---|---|---|
| Contents | Read and write | `deliver` pushes the ticket branch |
| Pull requests | Read and write | `deliver` opens the PR; `merge` reads/merges it |
| Checks | Read-only | merge gate reads PR check-runs to decide mergeability |
| Commit statuses | Read-only | fallback for legacy CI that uses commit statuses instead of check-runs |
| Actions | Read-only | CI monitor lists workflow runs and fetches job logs to file CI-failure tickets |
| Code scanning alerts | Read-only | `ci_fix` reads CodeQL alerts (rule, path, line) so it can fix the *actual* findings instead of blind-suppressing |
| Metadata | Read-only *(auto)* | required by GitHub for any App |

Leave everything else **No access**. Click **Create GitHub App**.

> Without `Code scanning alerts: Read`, the GitHub code-scanning API returns
> `403` and mill cannot see CodeQL alert details. A CodeQL-failing PR then has
> no findings to work from, so `ci_fix` may add wrong/blind suppression
> comments that don't clear the check. Grant this whenever CodeQL runs on a
> managed repo. (Granting a new permission to an existing App also requires
> approving the permission update on each installation.)

> If you intentionally disable the CI monitor (set `ci_monitor.enabled: false`
> per-repo in `config/repos.yaml` and never call `POST /ci-fix`), you can drop **Actions** to *No access*.
> Without `Actions: Read`, mill cannot fetch workflow-run statuses or job
> logs, and the CI monitor silently files tickets with empty logs — refine
> then has nothing concrete to work from and may confabulate root causes.
> Skip it deliberately, not accidentally.

### 2. Get the credentials

1. On the App page, copy the **App ID** (an integer).
2. **Private keys → Generate a private key** → a `.pem` downloads.
   Keep it safe.

### 3. Install the App on the repo

App page → **Install App** → install on your account → **Only select
repositories** → pick `<owner>/<repo>` → **Install**. (mill resolves the
installation automatically from `FORGE_REMOTE_URL`.)

### 4. Configure settings and secrets

```yaml
# config/config.json
forge:
  auth_mode: app
```

```yaml
# config/config.json
secrets:
  github_app_id: "<the integer App ID>"
  # Either point at the .pem file (recommended via bind mount):
  # github_app_private_key: ""  # leave empty when using path
  # …or inline the PEM (newlines as literal \n):
  github_app_private_key: "-----BEGIN RSA PRIVATE KEY-----\n...\n-----END..."
```

If using a `.pem` **file**, set `GITHUB_APP_PRIVATE_KEY_PATH`
environment variable and make sure the path is mounted into the
container (e.g. add a read-only bind in `docker-compose.override.yml`)
or place the key under `./.data` so it's already on the volume.

---

## Multi-repo installations

When serving multiple repos from a single mill process (see
`config/repos.yaml`), each repo entry can specify its own
`FORGE_REMOTE_URL`:

```yaml
# config/repos.yaml
repos:
  - repo_id: repo-a
    board_id: board-a
    FORGE_REMOTE_URL: https://github.com/owner-a/repo-a.git
    # … Langfuse keys …
  - repo_id: repo-b
    board_id: board-b
    FORGE_REMOTE_URL: https://github.com/owner-b/repo-b.git
    # … Langfuse keys …
```

When a ticket is processed, the mill resolves the target owner/repo
from the ticket's `repo_id` → `RepoConfig.forge_remote_url` instead of
the global `settings.forge_remote_url`. The installation ID is
discovered dynamically via
`GET /repos/{owner}/{repo}/installation` — the same mechanism works
for different repos under the same GitHub App, or for different Apps
if the operator registers separate credentials.

**To use different GitHub App installations per repo:**

1. Create and install the App(s) on each target repo as described in
   [Option B](#option-b--github-app-bot-recommended) above.
2. Set `FORGE_REMOTE_URL` on each repo entry in `config/repos.yaml`.
3. The mill automatically mints installation tokens for the correct
   repo at delivery time, caching them independently per
   `(app_id, remote_url)` pair.

> **Different Apps per repo**: if repos need separate GitHub App
> identities, register each App, install it on the relevant repo, and
> provide per-App credentials. Currently the mill uses a single
> `GITHUB_APP_ID` / private key pair (from the `config.json` `secrets:` block); per-repo
> App credentials are a future enhancement. For most deployments, one
> App installed on all repos is sufficient.

When a repo entry does **not** specify `FORGE_REMOTE_URL`, the mill
falls back to the global `settings.forge_remote_url` for backward
compatibility with single-repo deployments.

## Notes

- The minted installation token lives only in the **mill** process,
  cached ~50 min, used for the git push + the PR API call. The
  implement agent runs in the separate `--network none` sandbox and
  cannot read it (see [docker-architecture.md](docker-architecture.md)).
- `GITHUB_APP_PRIVATE_KEY*` and `FORGE_TOKEN` are secrets — keep them in
  the gitignored `config/config.json` `secrets:` block (or a mounted file); never commit them.
- GitHub Enterprise: set `MILL_GITHUB_API_URL=https://<host>/api/v3`.
- Bot commit authorship: the PR is authored by the bot; commit author
  is mill's git identity. Setting commits to the bot too is a future
  enhancement.
