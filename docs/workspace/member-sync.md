# Workspace member sync

**Source:** `src/robotsix_mill/workspace/member_sync.py`

The `sync_workspace_members()` function bridges the gap between a master
repo's vcs2l manifest and the mill's repo registry. It is called after
`detect_workspace_members()` (from `config.workspace_members`) parses a
master's git-submodule / workspace manifest.

## 1. Entry point

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

## 2. Per-member actions

For each `DetectedMember` discovered from the manifest:

| Action | Condition | Outcome |
|---|---|---|
| **Add** | No existing entry for `repo_id` | New `RepoConfig` stanza written; build-out ticket filed on the member's board |
| **Update** | Existing `member_of` entry from same master | Fields refreshed (URL, branch, cross-repo target); `pending_removal` flag cleared |
| **Skip** | Existing entry NOT from this master (`member_of` mismatch) | Left untouched — prevents clobbering manual config |
| **Flag vanished** | Existing `member_of` entry from same master, but member no longer in manifest | `pending_removal: true` set on the registry entry — board preserved for operator retirement |

## 3. Registry entry shape

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

## 4. `MemberSyncResult`

| Field | Type | Description |
|---|---|---|
| `added` | `list[str]` | Repo IDs registered for the first time |
| `updated` | `list[str]` | Existing member entries whose fields were refreshed |
| `flagged_for_removal` | `list[str]` | Members no longer in manifest, flagged `pending_removal: true` |
| `filed_tickets` | `dict[str, str]` | `repo_id → ticket_id` for build-out tickets filed |
| `skipped` | `list[str]` | Repo IDs that collided with a non-member entry |

## 5. Helper: `_member_repo_id(path)`

Lowercases the manifest path key and collapses every run of
non-alphanumeric characters (slashes, dots, spaces) into a single hyphen.
Example: `"src/zeta/pkg"` → `"src-zeta-pkg"`.

---

## See also

- [index.md](../index.md) — documentation home
- [docs/scm/forge/architecture.md](../scm/forge/architecture.md) — forge adapter design
- [docs/config/configuration.md](../config/configuration.md) — full env-var reference
