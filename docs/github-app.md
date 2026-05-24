# Delivery identity: GitHub App bot (recommended) or PAT

The `deliver` stage pushes the ticket branch and opens a Pull Request.
Two ways to authenticate, set via `FORGE_AUTH` in `config/mill.local.yaml`
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
# config/mill.local.yaml
forge:
  kind: github
  remote_url: https://github.com/<owner>/<repo>.git
  target_branch: main
```

## Option A — PAT (quick, for testing)

```yaml
# config/secrets.yaml
forge_token: <token>
```
With `forge.auth: token` (the default) in your settings.

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
| Metadata | Read-only *(auto)* | required by GitHub for any App |

Leave everything else **No access**. Click **Create GitHub App**.

> If you intentionally disable the CI monitor (`MILL_CI_MONITOR_PERIODIC=false`
> and never call `POST /ci-fix`), you can drop **Actions** to *No access*.
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
# config/mill.local.yaml
forge:
  auth: app
```

```yaml
# config/secrets.yaml
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

## Notes

- The minted installation token lives only in the **mill** process,
  cached ~50 min, used for the git push + the PR API call. The
  implement agent runs in the separate `--network none` sandbox and
  cannot read it (see [docker-architecture.md](docker-architecture.md)).
- `GITHUB_APP_PRIVATE_KEY*` and `FORGE_TOKEN` are secrets — keep them in
  the gitignored `config/secrets.yaml` (or a mounted file); never commit them.
- GitHub Enterprise: set `MILL_GITHUB_API_URL=https://<host>/api/v3`.
- Bot commit authorship: the PR is authored by the bot; commit author
  is mill's git identity. Setting commits to the bot too is a future
  enhancement.
