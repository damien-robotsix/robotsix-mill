"""Bridged git tools for sandboxed agents (rebase, ci_fix).

These tool closures execute HOST-SIDE — the mill's own process shells
out to ``git`` directly with the per-repo token and remote URL.  The
agent stays inside its ``--network none`` sandbox and never sees
credentials; the token is resolved via a provider callable at each
operation and never appears in tool args or the system prompt.

Every tool is guardrailed to operate ONLY on the ticket's own branch
and target — arbitrary branch/remote arguments are rejected.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..runtime.tracing import trace_stage
from ..vcs import git_ops

log = logging.getLogger(__name__)


def build_bridged_git_tools(
    *,
    repo_dir: Path,
    branch: str,
    target: str,
    remote_url: str,
    token: str | None = None,
    token_provider: Callable[[], str | None] | None = None,
    token_cache_clear: Callable[[], None] | None = None,
) -> list[Callable[..., Any]]:
    """Build the four bridged git tool closures.

    Each tool is a plain callable with type hints + docstring so
    pydantic-ai can derive its JSON schema.  They share the captured
    *repo_dir*, *branch*, *target*, *remote_url*, and token provider.

    Guardrails:
    - ``git_fetch`` only fetches *target* (the base branch, e.g. ``main``).
    - ``git_remote_sha`` / ``git_push_with_lease`` / ``git_branch_ancestry``
      only operate on *branch* (the ticket's PR branch).
    - The token is never returned in any tool output.

    Token resolution:
    - When *token_provider* is given, each operation calls it to obtain
      a fresh token.  This prevents stale-token failures on long-running
      agent sessions where the GitHub App installation token may expire.
    - When only *token* is given (backward compatibility), it is wrapped
      in a constant provider.
    - *token_cache_clear* is called before retrying a push that failed
      with an auth error, forcing the provider to mint a fresh token.
    """
    # Resolve the token provider: prefer the explicit callable, fall back
    # to wrapping the static token string.
    if token_provider is not None:
        _get_token = token_provider
    elif token is not None:
        _get_token = lambda: token
    else:
        _get_token = lambda: None

    def git_fetch(target_branch: str) -> str:
        """Fetch ``origin/<target_branch>`` to refresh the local
        remote-tracking ref. The agent calls this BEFORE rebasing so
        it works against current main, not a stale ref.

        Guardrailed: only the ticket's configured target branch is
        accepted — arbitrary branches are rejected.
        """
        with trace_stage("git_fetch"):
            if target_branch != target:
                return (
                    f"error: git_fetch is guardrailed to target branch "
                    f"'{target}' — '{target_branch}' rejected"
                )
            try:
                git_ops.fetch(
                    repo_dir,
                    remote_url=remote_url,
                    token=_get_token(),
                    branch=target_branch,
                )
            except subprocess.CalledProcessError as e:
                return f"error: git_fetch failed: {git_ops.redact_credentials(str(e))}"
            return f"fetched origin/{target_branch}"

    def git_remote_sha(branch_name: str) -> str:
        """Return the current remote SHA of the PR branch.

        Fetches the remote branch first so the returned SHA is fresh
        (not the stale local tracking ref).  Guardrailed: only the
        ticket's own branch is accepted.
        """
        with trace_stage("git_remote_sha"):
            if branch_name != branch:
                return (
                    f"error: git_remote_sha is guardrailed to ticket branch "
                    f"'{branch}' — '{branch_name}' rejected"
                )
            try:
                git_ops.fetch(
                    repo_dir,
                    remote_url=remote_url,
                    token=_get_token(),
                    branch=branch_name,
                )
            except subprocess.CalledProcessError as e:
                return f"error: fetch before remote_sha failed: {git_ops.redact_credentials(str(e))}"
            sha = git_ops.remote_branch_sha(repo_dir, branch_name)
            if sha is None:
                return "error: no remote branch (branch may not exist yet)"
            return sha

    def git_push_with_lease(branch_name: str) -> str:
        """Push the ticket branch with a FRESH compare-and-swap lease.

        Fetches the remote branch first so the lease ref is current
        at call time — no stale-lease race from a pre-computed SHA.
        Returns a structured token the agent can match on:

        - ``PUSH_OK`` — push succeeded.
        - ``LEASE_REJECTED: ...`` — the remote advanced since the lease
          was computed. The agent should inspect ancestry and retry
          (self-authored) or report FAILED (foreign push).
        - ``PUSH_AUTH_ERROR: ...`` — the push failed because of an
          authentication problem (expired/revoked token, invalid
          credentials).  The agent should report this as a classified
          diagnostic rather than a code defect.
        - ``PUSH_ERROR: ...`` — some other error (network, etc.).

        On an auth failure, the token cache is cleared and the push is
        retried once with a freshly minted token.  This handles the
        common case where a long-running agent session outlives the
        GitHub App installation token's 1-hour TTL.

        Guardrailed: only the ticket's own branch is accepted.
        """
        with trace_stage("git_push_with_lease"):
            if branch_name != branch:
                return (
                    f"error: git_push_with_lease is guardrailed to ticket branch "
                    f"'{branch}' — '{branch_name}' rejected"
                )

            push_token = _get_token()

            # Fresh lease: fetch the remote branch so the tracking ref is
            # current at call time.
            try:
                git_ops.fetch(
                    repo_dir,
                    remote_url=remote_url,
                    token=push_token,
                    branch=branch_name,
                )
            except subprocess.CalledProcessError as e:
                stderr = (
                    e.stderr.decode("utf-8", errors="replace")
                    if isinstance(e.stderr, bytes)
                    else str(e.stderr or "")
                )
                # First-push: remote branch doesn't exist yet.
                # push_with_lease already handles this via --force;
                # don't abort the push for a missing remote ref.
                if "couldn't find remote ref" not in stderr.lower():
                    return f"PUSH_ERROR: fetch before push failed: {git_ops.redact_credentials(str(e))}"
                # else: remote branch absent — fall through to push_with_lease
            try:
                git_ops.push_with_lease(repo_dir, branch_name, remote_url, push_token)
                return "PUSH_OK"
            except subprocess.CalledProcessError as e:
                stderr = (
                    e.stderr.decode("utf-8", errors="replace")
                    if isinstance(e.stderr, bytes)
                    else str(e.stderr or "")
                )
                if "stale" in stderr.lower() or "[rejected]" in stderr.lower():
                    return f"LEASE_REJECTED: {git_ops.redact_credentials(stderr)}"
                # Classify auth failures distinctly so the agent (and its
                # diagnostic runner) can distinguish a credential blind spot
                # from a code defect.
                classification = git_ops.classify_push_error(stderr)
                if classification == "auth":
                    # Retry once with a fresh token: clear the cache so the
                    # provider mints a new installation token, then re-fetch
                    # and re-push.  This handles the common case where the
                    # token expired between agent start and push time.
                    if token_cache_clear is not None:
                        log.info(
                            "git_push_with_lease: auth failure — "
                            "clearing token cache and retrying"
                        )
                        token_cache_clear()
                    fresh_token = _get_token()
                    if fresh_token != push_token:
                        try:
                            git_ops.fetch(
                                repo_dir,
                                remote_url=remote_url,
                                token=fresh_token,
                                branch=branch_name,
                            )
                            git_ops.push_with_lease(
                                repo_dir,
                                branch_name,
                                remote_url,
                                fresh_token,
                            )
                            return "PUSH_OK"
                        except subprocess.CalledProcessError as retry_e:
                            retry_stderr = (
                                retry_e.stderr.decode("utf-8", errors="replace")
                                if isinstance(retry_e.stderr, bytes)
                                else str(retry_e.stderr or "")
                            )
                            return f"PUSH_AUTH_ERROR: {git_ops.redact_credentials(retry_stderr)}"
                    return f"PUSH_AUTH_ERROR: {git_ops.redact_credentials(stderr)}"
                return f"PUSH_ERROR: {git_ops.redact_credentials(str(e))}"

    def git_branch_ancestry(branch_name: str, target_branch: str) -> str:
        """Return the commits the remote PR branch carries ahead of
        ``origin/<target>``, with author/committer info (JSON).

        Fetches both refs first.  The agent uses the author/committer
        fields to decide foreign-vs-self divergence: if every commit
        carries the mill's own author/committer email it is a prior
        self-rebase and safe to retry; any foreign author means a human
        pushed and the mill must NOT clobber it.

        Guardrailed: only the ticket's own branch and target are accepted.
        """
        with trace_stage("git_branch_ancestry"):
            if branch_name != branch:
                return (
                    f"error: git_branch_ancestry is guardrailed to ticket branch "
                    f"'{branch}' — '{branch_name}' rejected"
                )
            if target_branch != target:
                return (
                    f"error: git_branch_ancestry is guardrailed to target branch "
                    f"'{target}' — '{target_branch}' rejected"
                )
            try:
                git_ops.fetch(
                    repo_dir,
                    remote_url=remote_url,
                    token=_get_token(),
                    branch=branch_name,
                )
                git_ops.fetch(
                    repo_dir,
                    remote_url=remote_url,
                    token=_get_token(),
                    branch=target_branch,
                )
            except subprocess.CalledProcessError as e:
                return f"error: fetch before ancestry failed: {git_ops.redact_credentials(str(e))}"
            commits = git_ops.branch_ancestry(repo_dir, branch_name, target_branch)
            if not commits:
                return "(no commits ahead of target — branches are identical)"
            return json.dumps(commits, indent=2)

    return [git_fetch, git_remote_sha, git_push_with_lease, git_branch_ancestry]


# Register the bridged git tools in the system-wide capability catalog so
# the prompt-tool-consistency guard and smoke tests see them.
from .tool_registry import ToolInfo, ToolRegistry

ToolRegistry.register(
    ToolInfo(
        name="git_fetch",
        description="Fetch origin/<target_branch> to refresh the local remote-tracking ref. Guardrailed to the ticket's target branch.",
        category="git",
        parameters={"target_branch": "str"},
    )
)
ToolRegistry.register(
    ToolInfo(
        name="git_remote_sha",
        description="Return the current remote SHA of the PR branch. Fetches first so the SHA is fresh. Guardrailed to the ticket branch.",
        category="git",
        parameters={"branch_name": "str"},
    )
)
ToolRegistry.register(
    ToolInfo(
        name="git_push_with_lease",
        description="Push the ticket branch with a fresh compare-and-swap lease. Returns structured PUSH_OK / LEASE_REJECTED / PUSH_ERROR. Guardrailed to the ticket branch.",
        category="git",
        parameters={"branch_name": "str"},
    )
)
ToolRegistry.register(
    ToolInfo(
        name="git_branch_ancestry",
        description="Return commits the remote PR branch carries ahead of origin/<target> with author/committer info (JSON). Guardrailed to the ticket branch and target.",
        category="git",
        parameters={"branch_name": "str", "target_branch": "str"},
    )
)
