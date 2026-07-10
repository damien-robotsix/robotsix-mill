# Forge authentication

The forge auth layer (`src/robotsix_mill/forge/auth.py`) provides two
authentication modes for forge API calls.

## Modes

| Mode | `FORGE_AUTH` | Forges | Token lifetime |
|------|-------------|--------|----------------|
| **Static token** | `token` | GitHub, GitLab | Until revoked |
| **GitHub App** | `app` | GitHub only | ~1 hour (auto-refreshed) |

## Static token (`FORGE_AUTH=token`)

Set `FORGE_AUTH=token` (the default) and provide `Secrets.forge_token`.

```yaml
# config/config.yaml
forge:
  auth_mode: token
secrets:
  forge_token: "<token>"
```

The token is sent via:
- **GitHub**: `Authorization: Bearer {token}`
- **GitLab**: `PRIVATE-TOKEN: {token}`

Each adapter defines its own `_build_headers()` — `GitHubForge` uses the
Bearer header, `GitLabForge` uses `PRIVATE-TOKEN`.

### GitHub PAT scopes

A fine-grained PAT needs:
- **Contents: Read/write** — push ticket branches
- **Pull requests: Read/write** — open, read, and merge PRs

A classic PAT needs `repo` scope.

### GitLab token scopes

A personal or project access token needs:
- `api` — full API access (or `write_repository` + `read_api` for narrower scope)
- The token must belong to a user with at least **Developer** role on the project

## GitHub App (`FORGE_AUTH=app`)

GitHub-only. Mills a short-lived installation access token at deliver
time via JWT → installation lookup → access token.

```yaml
# config/config.yaml
forge:
  auth_mode: app
secrets:
  github_app_id: "<App ID>"
  github_app_private_key: "-----BEGIN RSA PRIVATE KEY-----\n...\n-----END..."
```

### Token minting flow

1. **JWT** — signed with the App private key (RS256), expires in 9 min.
   Issuer is the App ID.
2. **Installation lookup** — `GET /repos/{owner}/{repo}/installation`
   with `Authorization: Bearer {jwt}`.
3. **Access token** — `POST /app/installations/{iid}/access_tokens`
   returns a ~1 hour token.
4. **Caching** — keyed by `"{app_id}:{remote_url}"`, TTL 50 min.
   The cache prevents re-minting within a single deliver cycle.

### Required App permissions

| Permission | Access | Why |
|---|---|---|
| Contents | Read and write | Push ticket branches |
| Pull requests | Read and write | Open, read, merge PRs |
| Checks | Read-only | Read check-run statuses |
| Commit statuses | Read-only | Legacy commit-status fallback |
| Actions | Read-only | CI monitor: list runs, fetch job logs |
| Code scanning alerts | Read-only | Read CodeQL alert details |
| Metadata | Read-only | Required by GitHub |

## Per-repo tokens

When `RepoConfig.forge_remote_url` is set, the adapter resolves the
remote per-repo. GitHub App tokens are cached independently per
`(app_id, remote_url)` pair.

## Separate repo-creation token

`Secrets.forge_repo_create_token` is a separate PAT used only for
`create_repo()`. GitHub App installation tokens cannot create repos
under personal accounts. When set, it overrides the main token for
`POST /orgs/{owner}/repos` and `POST /user/repos`.

## See also

- [github-app.md](github-app.md) — step-by-step GitHub App setup guide
- [architecture.md](architecture.md) — full forge design document (§4 covers auth in detail)
