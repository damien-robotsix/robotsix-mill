# Delivery identity: GitHub App bot (recommended) or PAT

The `deliver` stage pushes the ticket branch and opens a Pull Request.
Two ways to authenticate, set via `FORGE_AUTH` in `.env`:

| `FORGE_AUTH` | Identity on the PR/commits | Setup |
|---|---|---|
| `token` | **you** (the PAT owner) | 1 line |
| `app` (recommended) | a **bot**: `<app-slug>[bot]` | one-time App creation |

The App path mints a short-lived **installation access token** at
deliver time (JWT signed with the App private key → installation
token). No GitHub Actions needed — mill does it in-process. Same bot
identity as robotsix-project.

---

## Common `.env`

```
FORGE_KIND=github
FORGE_REMOTE_URL=https://github.com/<owner>/<repo>.git
FORGE_TARGET_BRANCH=main
```

## Option A — PAT (quick, for testing)

```
FORGE_AUTH=token
FORGE_TOKEN=<token>
```

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

**Repository permissions** (least privilege — only what `deliver`
needs):

| Permission | Access |
|---|---|
| Contents | Read and write *(push the branch)* |
| Pull requests | Read and write *(open the PR)* |
| Metadata | Read-only *(auto)* |

Leave everything else **No access**. Click **Create GitHub App**.

### 2. Get the credentials

1. On the App page, copy the **App ID** (an integer).
2. **Private keys → Generate a private key** → a `.pem` downloads.
   Keep it safe.

### 3. Install the App on the repo

App page → **Install App** → install on your account → **Only select
repositories** → pick `<owner>/<repo>` → **Install**. (mill resolves the
installation automatically from `FORGE_REMOTE_URL`.)

### 4. Configure `.env`

```
FORGE_AUTH=app
GITHUB_APP_ID=<the integer App ID>
# Either point at the .pem file (recommended)…
GITHUB_APP_PRIVATE_KEY_PATH=/path/to/app.pem
# …or inline the PEM (newlines as literal \n):
# GITHUB_APP_PRIVATE_KEY=-----BEGIN RSA PRIVATE KEY-----\n...\n-----END...
```

If you point at a `.pem` **path**, make sure that path is mounted into
the container (e.g. add a read-only bind in `docker-compose.override.yml`)
or place the key under `./.data` so it's already on the volume.

---

## Notes

- The minted installation token lives only in the **mill** process,
  cached ~50 min, used for the git push + the PR API call. The
  implement agent runs in the separate `--network none` sandbox and
  cannot read it (see [docker-architecture.md](docker-architecture.md)).
- `GITHUB_APP_PRIVATE_KEY*` and `FORGE_TOKEN` are secrets — keep them in
  the gitignored `.env` (or a mounted file); never commit them.
- GitHub Enterprise: set `MILL_GITHUB_API_URL=https://<host>/api/v3`.
- Bot commit authorship: the PR is authored by the bot; commit author
  is mill's git identity. Setting commits to the bot too is a future
  enhancement.
