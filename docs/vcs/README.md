# VCS module — git CLI wrappers

**Source:** `src/robotsix_mill/vcs/git_ops.py`

The `vcs` module provides thin, synchronous wrappers around the `git`
CLI via `subprocess`. It is a leaf module with no dependencies on any
other `robotsix_mill` package. Every operation shells out to `git` —
the container image carries the binary, so no Python git library is
needed.

---

## Repository lifecycle

| Function | Purpose |
|---|---|
| `clone(remote_url, dest, branch, token)` | Shallow-clone a single branch into a workspace directory. Accepts an optional auth token for private repos. |
| `init_repo(dest, branch)` | Initialise an empty git repository (used for greenfield repos created by the mill). |

## Branch operations

| Function | Purpose |
|---|---|
| `checkout(repo, name)` | Switch to an existing branch. |
| `create_branch(repo, name)` | Create a new branch from HEAD. |
| `branch_exists(repo, name)` | Check whether a local branch exists. |

## Commit & staging

| Function | Purpose |
|---|---|
| `commit_all(repo, message)` | Stage all changes and commit with a message. |
| `commit_file(repo, filename, message)` | Stage and commit a single file; returns `False` if the file has no changes. |
| `has_changes(repo)` | Check whether the working tree has uncommitted modifications. |

## Push & fetch

| Function | Purpose |
|---|---|
| `push(repo, branch, remote_url, token)` | Force-push a branch to a remote. |
| `fetch(repo, remote_url, token, branch)` | Fetch a single branch from a remote. |
| `push_with_lease(repo, branch, remote_url, token, force=False)` | Compare-and-swap push: only succeeds if the remote ref matches the local expectation, protecting against concurrent pushes. Falls back to force-push when `force=True`. |
| `reconcile_with_remote_pr(repo, remote_url, token, branch, emails)` | Before push, reconcile the local branch with the remote PR branch. Detects foreign commits (human-authored) that a force-push would overwrite and returns `ReconcileResult.DIVERGED` in that case to prevent accidental data loss. |
| `post_push_check(repo, branch, remote_url, token)` | Verify that the remote branch matches the local branch after a push. Returns `PostPushResult.SYNCED`, `BEHIND`, or `UNAVAILABLE`. |

## Inspection & diff

| Function | Purpose |
|---|---|
| `head_sha(repo)` | Return the full SHA of HEAD. |
| `remote_branch_sha(repo, branch)` | Return the SHA of a remote tracking branch, or `None`. |
| `ls_remote_sha(remote_url, branch, token)` | Resolve a remote branch to its SHA via `git ls-remote`. |
| `branch_ancestry(repo, branch, target)` | Return the commit ancestry between two branches as a list of `{sha, message}` dicts. |
| `branch_is_ahead_of_main(repo, target_branch)` | True when the current branch has commits not in the target branch. |
| `branch_is_behind_main(repo, target_branch)` | True when the target branch has commits not in the current branch. |
| `branch_has_net_diff(repo, merge_base, target_branch)` | True when there is a diff between the current branch and a merge-base. |
| `changed_files(repo, target_branch)` | List files changed between the current branch and a target branch. |
| `introduced_files(repo, target_branch)` | List files added or modified (not deleted) between the current branch and a target branch. |
| `added_files(repo, target_branch)` | List files added (new, not previously tracked) between the current branch and a target branch. |
| `conflicted_files(repo)` | List files with merge conflicts in the working tree. |
| `diff_base(repo, target_branch, *, paths)` | Return the base commit for a diff (HEAD or the current branch tip). |

## Recovery & safety

| Function | Purpose |
|---|---|
| `try_rebase_onto(repo, branch)` | Attempt a rebase onto a branch. Returns `True` on success, `False` on conflict. |
| `restore_paths(repo, target_branch, paths)` | Restore specific files to their state in a target branch. |
| `ignored_paths(repo, paths)` | Filter a path list to those hidden by `.gitignore` rules. |
| `ignored_existing_paths(repo, paths)` | Same as `ignored_paths` but only for paths that actually exist on disk. |

## Utility

| Function | Purpose |
|---|---|
| `redact_credentials(text)` | Strip `://user:token@` credentials from URLs for log-safe output. |

## Enums

| Type | Purpose |
|---|---|
| `ReconcileResult` | `SYNCED`, `DIVERGED`, or `UNAVAILABLE` — outcome of `reconcile_with_remote_pr`. |
| `PostPushResult` | `SYNCED`, `BEHIND`, `DIVERGED`, or `UNAVAILABLE` — outcome of `post_push_check`. |

## See also

- [index.md](../index.md) — documentation home
- [docs/blocked-ticket-recovery.md](../blocked-ticket-recovery.md) — vcs-imported sub-repo guard
- [docs/configuration.md](../configuration.md) — vcs-related configuration settings
