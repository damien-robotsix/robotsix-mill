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
| `fetch(repo, *, remote_url, token, branch)` | Fetch a single branch from a remote. |
| `push_with_lease(repo, branch, remote_url, token)` | Compare-and-swap push: uses `--force-with-lease=<branch>:<expected-sha>` where `<expected-sha>` is the current `origin/<branch>` value from a prior fetch/reconcile. If the remote branch doesn't exist yet, falls back to plain `--force`. Raises `CalledProcessError` on lease violation. |
| `reconcile_with_remote_pr(repo, remote_url, branch, token)` | Before push, reconcile the local branch with the remote PR branch. Detects foreign commits (human-authored) that a force-push would overwrite and returns `ReconcileResult.DIVERGED` in that case to prevent accidental data loss. |
| `post_push_check(repo, branch, target, remote_url, token)` | Deterministic post-check after an agent-driven push. Fetches both the PR branch and `target` branch, verifies the remote HEAD matches local HEAD, and checks every ahead-of-target commit is mill-authored. Returns `PostPushResult.PASS`, `NOT_LANDED`, `FOREIGN_DIVERGENCE`, or `UNAVAILABLE`. Primarily called from the merge/review-revision stages, not from the push/fetch utility layer. |

## Inspection & diff

| Function | Purpose |
|---|---|
| `head_sha(repo)` | Return the full SHA of HEAD. |
| `remote_branch_sha(repo, branch)` | Return the SHA of a remote tracking branch, or `None`. |
| `ls_remote_sha(remote_url, ref, token)` | Resolve *ref* (default `HEAD`) on a remote to its SHA via `git ls-remote`. Returns `None` on any failure. |
| `branch_ancestry(repo, branch, target)` | Return commits on `origin/<branch>` not on `origin/<target>` as a list of `{sha, author_name, author_email, committer_name, committer_email, subject}` dicts. |
| `branch_is_ahead_of_main(repo, target_branch)` | True when the current branch has commits not in the target branch. |
| `branch_is_behind_main(repo, target_branch)` | True when the target branch has commits not in the current branch. |
| `branch_has_net_diff(repo, target_branch="main", ref="HEAD")` | True when *ref* has a non-empty content diff vs ``origin/main`` (three-dot diff against the merge-base). |
| `changed_files(repo, target_branch)` | List files changed between the current branch and a target branch. |
| `introduced_files(repo, target_branch)` | List files added or modified (not deleted) between the current branch and a target branch. |
| `added_files(repo, target_branch)` | List files added (new, not previously tracked) between the current branch and a target branch. |
| `conflicted_files(repo)` | List files with merge conflicts in the working tree. |
| `diff_base(repo, target_branch, *, remote_url, token)` | Return the unified diff of all commits on the current branch vs origin/`target_branch`. Fetches first so the diff is current. |

## Recovery & safety

| Function | Purpose |
|---|---|
| `try_rebase_onto(repo, target, *, remote_url, token)` | Attempt a rebase onto *target*. Fetches the target branch fresh and rebases the current branch onto it. Returns `True` on success, `False` on conflict (with the rebase aborted). |
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
| `PostPushResult` | `PASS`, `NOT_LANDED`, `FOREIGN_DIVERGENCE`, or `UNAVAILABLE` — outcome of `post_push_check`. |

## See also

- [index.md](../index.md) — documentation home
- [docs/blocked-ticket-recovery.md](../stages/blocked-ticket-recovery.md) — vcs-imported sub-repo guard
- [docs/configuration.md](../configuration.md) — vcs-related configuration settings
