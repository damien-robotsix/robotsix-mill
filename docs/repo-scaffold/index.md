# Repo-scaffold — automated repository creation and member sync

**Source:** `src/robotsix_mill/repo_scaffold/`

The `repo-scaffold` module automates the creation and registration of new
repositories and the auto-discovery of workspace members. It straddles the
forge, VCS, and config boundaries: it calls the forge adapter to create a
remote repo, uses `git_ops` to push an initial scaffold commit, and appends
a `RepoConfig` entry to the machine-owned repos overlay.

---

## 1. Repo creation workflow (`run_repo_scaffold`)

When a meta-board ticket carries a `new-repo` extraction marker, the
maintenance agent calls `run_repo_scaffold()` with creation parameters and
the raw ticket description.

**Entry point:** `src/robotsix_mill/repo_scaffold/__init__.py`

### 1.1 Parameters

| Key | Type | Required | Description |
|---|---|---|---|
| `name` | `str` | Yes | Repository name (e.g. `robotsix-foo`) |
| `owner` | `str` | Yes | Forge organisation or user |
| `private` | `bool` | Yes | Whether the repo is private |
| `description` | `str` | No | Short description / purpose |
| `language` | `str` | No | Default `"python"` — determines scaffold content |

### 1.2 Workflow steps

1. **Create remote repo** — `Forge.create_repo(name, owner, private, description)`.
   Returns a `RepoInfo` with `id`, `name`, `clone_url`, `html_url`.

2. **Scaffold initial commit** (`_scaffold_initial_commit`):
   - `git init` a temp workspace on the forge's default branch.
   - Write `README.md` (name + description).
   - Write `LICENSE` (MIT, same as the robotsix-mill root).
   - **Python repos** (`language == "python"`):
     - `pyproject.toml` with hatchling build system + explicit wheel target.
     - `src/<import_safe_name>/__init__.py` (hyphens → underscores).
     - `tests/__init__.py`.
     - `.github/workflows/ci.yml` — reusable-workflow caller (`python-ci.yml`).
     - `.github/workflows/docs.yml` — reusable-workflow caller (`python-docs.yml`).
   - Write `.robotsix-mill/config.yaml` — the repo owns its `test_command`
     and `languages` (not the operator's `repos.yaml`).
   - Write `.robotsix-mill/periodic/{audit,health}.yaml` presence files —
     file presence opts these periodic agents in for the new repo.
   - Stage, commit (`"Initial scaffold"`), force-push.

3. **Register in repos overlay** (`_append_repo_config`):
   - Derives a `repo_id` via `_sanitize_repo_id(name)`.
   - Appends a `RepoConfig` stanza to `<data_dir>/registered_repos.yaml`
     (or honours `MILL_REPOS_FILE`).
   - Hot-reloads the config singleton via `_reset_repos_config()`.

4. **File build-out ticket** (`_file_implementation_followup`):
   - Files a ticket titled `"Implement <name>: initial build-out"` on the
     new repo's own board (`board_id = sanitized repo name`).
   - The ticket spec is derived from the scaffold ticket's purpose
     (description minus the `new-repo` marker).
   - Best-effort: a failure here does not roll back the (already succeeded)
     repo creation + registration.

### 1.3 Error handling

| Condition | Outcome |
|---|---|
| Forge not configured (`NotConfiguredError`) | `BLOCKED` |
| Repo already exists (`RuntimeError` with "already exists") | `BLOCKED` |
| Scaffold commit fails | `ERRORED` |
| Repos overlay append fails | `ERRORED` |
| Build-out ticket filing fails | Logged, not failed — repo is still created |

### 1.4 Helper: `_sanitize_repo_id(name)`

Lowercases the name, collapses every run of non-alphanumeric characters
(spaces, underscores, dots, non-ASCII) into a single hyphen, and strips
leading/trailing hyphens.  Falls back to the raw `name.lower()` if the
result is empty.

---

## 2. Workspace member sync (`member_sync.py`)

**Source:** `src/robotsix_mill/repo_scaffold/member_sync.py`

The `sync_workspace_members()` function bridges the gap between a master
repo's vcs2l manifest and the mill's repo registry. It is called after
`detect_workspace_members()` (from `config.workspace_members`) parses a
master's git-submodule / workspace manifest.

### 2.1 Entry point

```python
sync_workspace_members(
    settings,
    master_repo_id,
    members,          # Iterable[DetectedMember]
    *,
    repos_yaml_path=None,
    file_tickets=True,
) -> MemberSyncResult
```

### 2.2 Per-member actions

For each `DetectedMember` discovered from the manifest:

| Action | Condition | Outcome |
|---|---|---|
| **Add** | No existing entry for `repo_id` | New `RepoConfig` stanza written; build-out ticket filed on the member's board |
| **Update** | Existing `member_of` entry from same master | Fields refreshed (URL, branch, cross-repo target); `pending_removal` flag cleared |
| **Skip** | Existing entry NOT from this master (`member_of` mismatch) | Left untouched — prevents clobbering manual config |
| **Flag vanished** | Existing `member_of` entry from same master, but member no longer in manifest | `pending_removal: true` set on the registry entry — board preserved for operator retirement |

### 2.3 Registry entry shape

Each synced entry carries:

```yaml
repos:
  <repo_id>:
    board_id: <repo_id>
    forge_remote_url: <member.url>
    member_of: <master_repo_id>     # provenance marker
    working_branch: <version>       # from manifest version
    cross_repo_target: {...}        # upstream fork policy (optional)
```

Langfuse configuration is inherited from the global top-level `langfuse`
block — no per-repo stanza is written.

### 2.4 `MemberSyncResult`

| Field | Type | Description |
|---|---|---|
| `added` | `list[str]` | Repo IDs registered for the first time |
| `updated` | `list[str]` | Existing member entries whose fields were refreshed |
| `flagged_for_removal` | `list[str]` | Members no longer in manifest, flagged `pending_removal: true` |
| `filed_tickets` | `dict[str, str]` | `repo_id → ticket_id` for build-out tickets filed |
| `skipped` | `list[str]` | Repo IDs that collided with a non-member entry |

### 2.5 Helper: `_member_repo_id(path)`

Lowercases the manifest path key and collapses every run of
non-alphanumeric characters (slashes, dots, spaces) into a single hyphen.
Example: `"src/zeta/pkg"` → `"src-zeta-pkg"`.

---

## See also

- [index.md](../index.md) — documentation home
- [docs/forge/architecture.md](../forge/architecture.md) — forge adapter design
- [docs/vcs/README.md](../vcs/README.md) — `git_ops` CLI wrappers
- [docs/configuration.md](../configuration.md) — full env-var reference
