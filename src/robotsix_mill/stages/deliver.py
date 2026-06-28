"""Deliver stage: DELIVERABLE -> IMPLEMENT_COMPLETE.

Push the ticket's branch to the configured forge and open a PR/MR
against ``FORGE_TARGET_BRANCH``. The forge adapter only does the API
call; this stage owns the git push (it has the workspace clone).

When ``MILL_PR_SUMMARY_ENABLED`` is true, the stage generates a
structured PR body (Summary / Changes / Test Plan) from the
implementation diff via a cheap one-shot LLM call, falling back to
the raw spec on error.  Otherwise the raw spec is used as-is.

Anything that isn't success is BLOCKED-resumable (move back to
DELIVERABLE to retry) — never terminal FAILED, so a transient forge/
network problem doesn't lose the implemented branch. The PR URL is
recorded in history + an artifact.

Multi-repo delivery (meta-board tickets)
----------------------------------------

When the implement stage produced a multi-repo workspace, it writes a
``touched_repos.json`` manifest into ``artifacts/`` listing every
repo that received a commit::

    [
      {"repo_id": str, "branch": str, "repo_path": str},
      ...
    ]

The deliver stage detects that manifest and iterates it, pushing each
repo's branch and opening one PR per repo via that repo's
``RepoConfig``.  The resulting PR URLs are written to a second
artifact, ``pr_urls.json``, with the schema::

    [
      {"repo_id": str, "branch": str, "url": str},
      ...
    ]

``pr_urls.json`` is the seam downstream stages (merge polling,
``_pr_url`` enrichment) will consume to find all PRs opened for a
single meta ticket.  It is written ONLY in the multi-repo branch and
ONLY after at least one PR succeeds; its presence is the multi-repo
discriminator for downstream stages.  The file is rewritten atomically
(``.json.tmp`` → ``os.replace``) after every successful PR so a
mid-loop BLOCKED leaves a consistent partial manifest the resume run
can read.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from pathlib import Path

from ..agents.base import build_agent
from ..agents.retry import run_agent
from ..config import get_repo_config, target_branch_for
from ..config import ConfigError
from ..core.models import Ticket
from ..core.states import State
from ..forge import get_forge
from ..forge.auth import _resolve_remote_url, github_token
from ..forge.github import _parse_owner_repo
from ..vcs import git_ops
from .base import Outcome, Stage, StageContext

log = logging.getLogger("robotsix_mill.stages.deliver")

PR_SUMMARY_SYSTEM_PROMPT = """\
You are a technical PR description writer. Given an implementation diff \
and the original ticket spec, produce a structured PR body with exactly \
three sections:

## Summary
One paragraph summarising what this PR does at a high level. Focus on \
the concrete changes, not the intent.

## Changes
A bullet list of the specific files and modifications made. Be precise \
and concrete — mention file paths and what was changed.

## Test Plan
A short list of verification steps a reviewer or CI should run to \
confirm correctness. When the diff includes tests, note that.

Output only the Markdown body — no preamble, no code fences."""

PR_SUMMARY_USER_TEMPLATE = """\
## Ticket Spec
{spec}

## Implementation Diff
{diff}

Generate the structured PR body."""


def generate_pr_description(
    diff: str,
    spec: str,
    settings: "Settings",  # noqa: F821 — forward reference
) -> str:
    """Generate a structured PR body from the implementation diff.

    Falls back to *spec* when PR summary is disabled or the LLM call fails.
    """
    if not settings.pr_summary_enabled:
        return spec

    truncated = False
    diff_text = diff
    if len(diff_text) > 16_000:
        diff_text = diff_text[:16_000]
        truncated = True

    try:
        agent = build_agent(
            settings,
            system_prompt=PR_SUMMARY_SYSTEM_PROMPT,
            output_type=str,
            level=1,
            report_issue=False,
            read_ticket=False,
            reply_to_thread=False,
            close_thread=False,
            ask_user=False,
            retries=1,
        )
        prompt = PR_SUMMARY_USER_TEMPLATE.format(
            spec=spec[:4000],
            diff=diff_text,
        )
        result = run_agent(
            agent,
            lambda h: h.run_sync(prompt),
            what="PR summary generation",
        )
        body = result.data.strip()
        if truncated:
            body += (
                "\n\n> ⚠️ The diff was truncated to 16 000 characters for this summary."
            )
        return body
    except Exception:
        log.exception("PR summary generation failed; falling back to raw spec")
        return spec


def _meta_triage_was_fallback(artifacts_dir: Path) -> bool:
    """True when meta repo-triage fell back to cloning *every* repo.

    Reads the ``meta_triage.json`` artifact written by
    :func:`robotsix_mill.meta.workspace.build_triaged_meta_workspace`.
    Returns ``False`` when the artifact is absent or unreadable (the
    safe default — a missing signal must not block delivery), and when
    triage confidently matched a repo or the ticket genuinely targets
    all repos.
    """
    path = artifacts_dir / "meta_triage.json"
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return False
    return isinstance(data, dict) and bool(data.get("fallback"))


def _write_pr_urls(artifacts_dir: Path, entries: list[dict]) -> None:
    """Atomically (re)write the ``pr_urls.json`` manifest.

    Uses ``.json.tmp`` → :func:`os.replace` so a mid-loop crash never
    leaves a half-written file on disk.  Called after every successful
    PR in the multi-repo branch.

    Schema::

        [
          {"repo_id": str, "branch": str, "url": str},
          ...
        ]
    """
    target = artifacts_dir / "pr_urls.json"
    tmp = artifacts_dir / "pr_urls.json.tmp"
    tmp.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    os.replace(tmp, target)


# --- merge-guard loop detection (fingerprint + counter) ---

_DELIVER_MERGE_GUARD_FINGERPRINT = "deliver_merge_guard_fingerprint.txt"
_DELIVER_MERGE_GUARD_IDENTICAL_COUNT = "deliver_merge_guard_identical_count.txt"


def _read_counter(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except FileNotFoundError, ValueError:
        return 0


def _write_counter(path: Path, value: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(value), encoding="utf-8")


def _merge_guard_block_fingerprint(block_note: str, repo_ids: list[str]) -> str:
    """Stable hex fingerprint for a merge-guard block reason.

    Combines the sorted repo_ids with the block note text and hashes
    with SHA-256; the first 16 hex digits become the fingerprint.
    """
    data = "\n".join(sorted(repo_ids)) + "\n" + block_note
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


def _regen_uv_lock(repo_dir: Path, ticket_id: str) -> None:
    """Run ``uv lock`` and commit uv.lock if changed. Warn-and-proceed on failure."""
    try:
        result = subprocess.run(
            ["uv", "lock"],  # noqa: S607
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except Exception:
        log.warning(
            "%s: uv lock failed (uv not available); "
            "proceeding with existing uv.lock — CI will catch a stale lock",
            ticket_id,
        )
        return

    if result.returncode != 0:
        log.warning(
            "%s: uv lock failed (exit %s); "
            "proceeding with existing uv.lock — CI will catch a stale lock: %s",
            ticket_id,
            result.returncode,
            (result.stderr or "")[:500],
        )
        return

    try:
        if git_ops.commit_file(repo_dir, "uv.lock", "chore(deps): sync uv.lock"):
            log.info("%s: committed updated uv.lock", ticket_id)
    except subprocess.CalledProcessError as e:
        log.warning(
            "%s: failed to commit uv.lock: %s",
            ticket_id,
            e,
        )


def _regen_npm_lock(repo_dir: Path, ticket_id: str) -> None:
    """Run ``npm install --package-lock-only`` and commit package-lock.json
    if changed. Warn-and-proceed on failure."""
    try:
        result = subprocess.run(
            ["npm", "install", "--package-lock-only", "--no-audit", "--no-fund"],  # noqa: S607
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except Exception:
        log.warning(
            "%s: npm install failed (npm not available); "
            "proceeding with existing package-lock.json — CI will catch a stale lock",
            ticket_id,
        )
        return

    if result.returncode != 0:
        log.warning(
            "%s: npm install failed (exit %s); "
            "proceeding with existing package-lock.json — CI will catch a stale lock: %s",
            ticket_id,
            result.returncode,
            (result.stderr or "")[:500],
        )
        return

    try:
        if git_ops.commit_file(
            repo_dir, "package-lock.json", "chore(deps): sync package-lock.json"
        ):
            log.info("%s: committed updated package-lock.json", ticket_id)
    except subprocess.CalledProcessError as e:
        log.warning(
            "%s: failed to commit package-lock.json: %s",
            ticket_id,
            e,
        )


def _regen_lockfiles(repo_dir: Path, target: str, ticket_id: str) -> None:
    """Regenerate stale lockfiles on the branch before push.

    Checks which manifest files are in the net branch diff and regenerates
    only the corresponding lockfile when the lockfile already exists in the
    repo. Failures are non-fatal: a warning is logged and delivery proceeds
    (CI remains the backstop for a genuinely stale lock).
    """
    changed = git_ops.changed_files_net(repo_dir, target)
    if "pyproject.toml" in changed and (repo_dir / "uv.lock").exists():
        _regen_uv_lock(repo_dir, ticket_id)
    if "package.json" in changed and (repo_dir / "package-lock.json").exists():
        _regen_npm_lock(repo_dir, ticket_id)


class DeliverStage(Stage):
    """Push the implemented branch to the remote forge for review."""

    name = "deliver"
    input_state = State.DELIVERABLE
    traced = False

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Push the implemented branch to the remote forge and open (or update) a pull/merge request, transitioning the ticket toward review."""
        s = ctx.settings
        if s.forge_kind == "none":
            return Outcome(State.BLOCKED, "FORGE_KIND not configured")

        # Global remote/token gate: in the multi-repo branch each repo
        # is re-resolved per entry, but we still need a valid forge
        # configuration to even attempt delivery.  Use the
        # ctx.repo_config for the early-exit guards so the existing
        # single-repo error messages are unchanged.
        remote_url = _resolve_remote_url(s, ctx.repo_config)
        if not remote_url:
            return Outcome(State.BLOCKED, "FORGE_REMOTE_URL not configured")
        try:
            github_token(
                s, repo_config=ctx.repo_config
            )  # PAT or minted App installation token
        except RuntimeError as e:
            return Outcome(State.BLOCKED, f"forge auth not configured: {e}")

        ws = ctx.service.workspace(ticket)
        touched_path = ws.artifacts_dir / "touched_repos.json"

        if touched_path.exists():
            try:
                touched_repos = json.loads(touched_path.read_text(encoding="utf-8"))
            except OSError, ValueError:
                return Outcome(
                    State.BLOCKED,
                    "touched_repos.json corrupted — resumable",
                )
            return DeliverStage._run_multi_repo(ctx, ticket, s, touched_repos)

        return DeliverStage._run_single_repo(ctx, ticket, s)

    # ------------------------------------------------------------------
    # Single-repo branch (unchanged behaviour — the existing code path).
    # ------------------------------------------------------------------

    @staticmethod
    def _run_single_repo(ctx: StageContext, ticket: Ticket, s) -> Outcome:
        """The existing, single-repo delivery path.

        Routes one branch in the workspace clone at ``ws.dir/"repo"``
        through the per-repo helper.  Preserves the byte-identical
        outcome notes / artifact format that downstream consumers and
        tests rely on.
        """
        ws = ctx.service.workspace(ticket)
        repo_dir = ws.dir / "repo"
        branch = ticket.branch or f"{s.branch_prefix}{ticket.id}"
        target = target_branch_for(s, ctx.repo_config)
        if not (repo_dir / ".git").exists() or not git_ops.branch_exists(
            repo_dir, branch
        ):
            return Outcome(
                State.BLOCKED,
                "no implemented branch to deliver (re-run implement)",
            )

        url, sub_outcome = DeliverStage._deliver_one_repo(
            ctx, ticket, s, repo_dir, branch, ctx.repo_config
        )

        if sub_outcome is not None:
            # The helper translates a 422 "No commits between" from the
            # forge into a BLOCKED outcome whose note carries that
            # phrase verbatim. Detect it here and re-route to DONE — the
            # same conclusion the local ahead-of-main guard reaches —
            # rather than looping forever in BLOCKED-resumable.
            if sub_outcome.next_state is State.BLOCKED and "No commits between" in (
                sub_outcome.note or ""
            ):
                log.info(
                    "%s: forge reports no commits between %s and the branch — "
                    "routing to DONE (nothing to deliver)",
                    ticket.id,
                    target,
                )
                return Outcome(
                    State.DONE,
                    "no change needed — the forge reports no commits between "
                    f"{target} and the branch; there is nothing "
                    "to deliver",
                )
            return sub_outcome

        if url is None:
            # Skipped by the ahead-of-main / net-diff guard. Mirrors
            # refine's ``no_change_needed`` bypass — same shape, just
            # one stage later.
            return Outcome(
                State.DONE,
                "no change needed — branch contains no new commits vs "
                f"{target}; the spec was already satisfied "
                "by the current codebase",
            )

        (ws.artifacts_dir / "deliver.md").write_text(
            f"# Deliver (passed)\nbranch: {branch}\nPR: {url}\n",
            encoding="utf-8",
        )
        log.info("%s: delivered → %s", ticket.id, url)
        # PR opened — gates not yet verified; merge stage will poll
        return Outcome(State.IMPLEMENT_COMPLETE, f"PR: {url}")

    # ------------------------------------------------------------------
    # Multi-repo branch (meta-board tickets with N≥1 touched repos).
    # ------------------------------------------------------------------

    @staticmethod
    def _run_multi_repo(
        ctx: StageContext,
        ticket: Ticket,
        s,
        touched_repos: list[dict],
    ) -> Outcome:
        """Iterate the touched-repos manifest, opening one PR per repo.

        Writes ``pr_urls.json`` incrementally (atomic replace) after
        every successful PR so a mid-loop BLOCKED leaves a consistent
        partial manifest the resume run can read.

        An empty manifest (``[]``) means implement determined no repo
        needed changes — route straight to DONE without touching the
        forge.
        """
        ws = ctx.service.workspace(ticket)

        if not touched_repos:
            return Outcome(
                State.DONE,
                "no change needed — no repos were modified by implement",
            )

        # Safety guard: when triage could NOT confidently match a target
        # repo (it fell back to cloning every repo), refuse to merge
        # brand-new top-level files into an arbitrarily-chosen primary
        # repo — they most likely belong to a repo that does not exist
        # yet.  Genuine all-repos tickets (the agent explicitly named
        # every repo) are not flagged as a fallback and proceed.
        #
        # Loop-detection: when the same merge-guard block fires
        # consecutively without progress (the identical brand-new
        # top-level file is detected each cycle), the fingerprint
        # is unchanged.  After ``deliver_max_identical_blocks``
        # consecutive identical blocks the stage escalates to a
        # stronger BLOCKED that requires human intervention instead
        # of burning cost on a deterministic resume→block loop.
        if _meta_triage_was_fallback(ws.artifacts_dir):
            blocked_repo_ids: list[str] = []
            blocked_files: dict[str, list[str]] = {}
            for entry in touched_repos:
                repo_id = entry.get("repo_id", "")
                repo_dir = Path(entry.get("repo_path", ""))
                try:
                    rc = get_repo_config(repo_id)
                except ConfigError:
                    continue
                if not (repo_dir / ".git").exists():
                    continue
                target = target_branch_for(s, rc)
                new_top_level = sorted(
                    f for f in git_ops.added_files(repo_dir, target) if "/" not in f
                )
                if new_top_level:
                    blocked_repo_ids.append(repo_id)
                    blocked_files[repo_id] = new_top_level

            if blocked_repo_ids:
                # Build the block note deterministically — the same
                # set of repos + files must produce the same text so
                # the fingerprint is stable across resume cycles.
                detail_parts: list[str] = []
                for rid in sorted(blocked_repo_ids):
                    files = blocked_files[rid]
                    detail_parts.append(f"{rid}: {', '.join(files)}")
                block_note = (
                    "meta target repo could not be determined — does it "
                    "need a not-yet-created repo? Refusing to merge "
                    f"brand-new top-level file(s): {'; '.join(detail_parts)}"
                )

                # Fingerprint-based loop detection.
                max_identical = s.deliver_max_identical_blocks
                if max_identical > 0:
                    current_fp = _merge_guard_block_fingerprint(
                        block_note, sorted(blocked_repo_ids)
                    )
                    artifacts = ws.artifacts_dir
                    fp_path = artifacts / _DELIVER_MERGE_GUARD_FINGERPRINT
                    counter_path = artifacts / _DELIVER_MERGE_GUARD_IDENTICAL_COUNT

                    stored_fp = ""
                    try:
                        stored_fp = fp_path.read_text(encoding="utf-8").strip()
                    except FileNotFoundError:
                        pass

                    if current_fp == stored_fp and stored_fp:
                        # Same block as last cycle — increment counter.
                        count = _read_counter(counter_path) + 1
                        _write_counter(counter_path, count)
                        if count >= max_identical:
                            return Outcome(
                                State.BLOCKED,
                                f"Merge guard blocked {count} consecutive "
                                "times with the same fingerprint "
                                f"({current_fp}) — the same brand-new "
                                "top-level file(s) were detected each "
                                "cycle without progress. Manual "
                                "intervention required: either create "
                                "the target repo, move the files into a "
                                "subdirectory, or add the files to an "
                                "existing repo config.\n\n"
                                f"Block reason: {block_note}",
                            )
                        # Below threshold — normal BLOCKED, will retry.
                        return Outcome(State.BLOCKED, block_note)

                    # Fingerprint changed (or first run) — store the
                    # new fingerprint and reset counter to 1 (this is
                    # the first occurrence of this particular block).
                    fp_path.parent.mkdir(parents=True, exist_ok=True)
                    fp_path.write_text(current_fp, encoding="utf-8")
                    _write_counter(counter_path, 1)

                return Outcome(State.BLOCKED, block_note)

        opened: list[dict] = []
        skipped: list[str] = []
        skipped_targets: dict[str, str] = {}

        for entry in touched_repos:
            repo_id = entry.get("repo_id", "")
            branch = entry.get("branch", "")
            repo_path_str = entry.get("repo_path", "")
            repo_dir = Path(repo_path_str)

            # Per-repo config lookup.
            try:
                rc = get_repo_config(repo_id)
            except ConfigError as e:
                return Outcome(
                    State.BLOCKED,
                    f"unknown repo_id '{repo_id}' in touched_repos.json — "
                    f"resumable: {e}",
                )

            # Per-repo target branch (each repo may override working_branch).
            target = target_branch_for(s, rc)

            # Workspace clone must still be present from implement's pass.
            if not (repo_dir / ".git").exists() or not git_ops.branch_exists(
                repo_dir, branch
            ):
                return Outcome(
                    State.BLOCKED,
                    f"no implemented branch in {repo_id} — re-run implement",
                )

            url, sub_outcome = DeliverStage._deliver_one_repo(
                ctx, ticket, s, repo_dir, branch, rc
            )

            if sub_outcome is not None:
                # BLOCKED outcome from the helper — any partial
                # pr_urls.json entries already written for earlier
                # repos are preserved on disk by the atomic-replace
                # write below.
                return sub_outcome

            if url is None:
                # Skipped by the ahead-of-main / net-diff guard — no
                # commits to ship for this repo. Record so the
                # summary artifact can name it.
                skipped.append(repo_id)
                skipped_targets[repo_id] = target
                log.info(
                    "%s: skipped %s — no commits ahead of %s",
                    ticket.id,
                    repo_id,
                    target,
                )
                continue

            opened.append({"repo_id": repo_id, "branch": branch, "url": url})
            # Atomic-replace incremental write after every successful PR
            # so a mid-loop failure preserves the partial manifest.
            _write_pr_urls(ws.artifacts_dir, opened)
            log.info("%s: delivered %s → %s", ticket.id, repo_id, url)

        if not opened:
            # Every touched repo was skipped — nothing to ship. Do NOT
            # write pr_urls.json (an empty file would confuse
            # downstream).
            return Outcome(
                State.DONE,
                f"no change needed — all touched repos already at {target}",
            )

        # Build the deliver.md summary with one line per touched repo.
        lines = ["# Deliver (passed)", "repos:"]
        opened_by_id = {entry["repo_id"]: entry for entry in opened}
        for entry in touched_repos:
            rid = entry.get("repo_id", "")
            if rid in opened_by_id:
                br = opened_by_id[rid]["branch"]
                url = opened_by_id[rid]["url"]
                lines.append(f"  - {rid}: branch={br}, PR={url}")
            elif rid in skipped:
                lines.append(
                    f"  - {rid}: SKIPPED (no commits vs {skipped_targets[rid]})"
                )
        (ws.artifacts_dir / "deliver.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

        urls = [entry["url"] for entry in opened]
        return Outcome(State.IMPLEMENT_COMPLETE, f"PRs: {', '.join(urls)}")

    # ------------------------------------------------------------------
    # Per-repo helper shared by both branches.
    # ------------------------------------------------------------------

    @staticmethod
    def _deliver_one_repo(
        ctx: StageContext,
        ticket: Ticket,
        s,
        repo_dir: Path,
        branch: str,
        repo_config,
    ) -> tuple[str | None, Outcome | None]:
        """Push *branch* and open one PR for the repo at *repo_dir*.

        Returns ``(pr_url, None)`` on success, ``(None, None)`` when
        the ahead-of-main / net-diff guard fires (skipped — nothing
        to deliver for this repo), or ``(None, Outcome(BLOCKED, ...))``
        on a real failure.

        The caller decides how to translate each shape into a stage
        outcome / manifest entry.  This helper writes ``pr_urls.json``
        in neither case — that's the multi-repo caller's job (see
        :func:`_write_pr_urls`).

        ``pr_urls.json`` schema (written by the caller)::

            [
              {"repo_id": str, "branch": str, "url": str},
              ...
            ]
        """
        repo_label = repo_config.repo_id if repo_config is not None else "default"
        target = target_branch_for(s, repo_config)

        # Cross-repo target: when configured, the branch is pushed to the
        # fork and the PR is opened fork→upstream (see CrossRepoTarget).
        cross_repo_target = getattr(repo_config, "cross_repo_target", None)

        # Per-repo forge inputs. For a cross-repo target the push goes to
        # the fork, not the clone remote.
        if cross_repo_target is not None:
            remote_url = cross_repo_target.fork_remote_url
        else:
            remote_url = _resolve_remote_url(s, repo_config)
        try:
            token = github_token(s, repo_config=repo_config)
        except RuntimeError as e:
            return None, Outcome(
                State.BLOCKED,
                f"forge auth not configured for {repo_label}: {e}",
            )

        # Guard: skip when the branch has no NET diff relative to
        # origin/main. This avoids a 422 "No commits between main and
        # branch" from GitHub. Two distinct cases both produce that 422:
        #   1. the branch carries no commits ahead of main at all
        #      (``branch_is_ahead_of_main`` is False); and
        #   2. the branch carries a commit (ahead by commit count)
        #      whose net content is identical to main — e.g. main
        #      independently landed the same change, or the commit is
        #      a no-op. ``branch_has_net_diff`` is what actually
        #      matches the forge's own emptiness test.
        if not git_ops.branch_is_ahead_of_main(
            repo_dir, target
        ) or not git_ops.branch_has_net_diff(repo_dir, target):
            return None, None

        # Cross-repo auto-fork: ensure the fork exists before pushing to
        # it. Opt-in (default False), mirroring enable_repo_creation's
        # conservative default.
        if cross_repo_target is not None and cross_repo_target.auto_fork:
            try:
                up_owner, up_repo = _parse_owner_repo(
                    cross_repo_target.upstream_remote_url
                )
                get_forge(s, repo_config=repo_config).fork_repo(
                    source_owner=up_owner, source_repo=up_repo
                )
            except Exception as e:  # noqa: BLE001 — resumable, don't lose branch
                log.exception("%s: auto-fork failed for %s", ticket.id, repo_label)
                return None, Outcome(
                    State.BLOCKED,
                    f"auto-fork failed for {repo_label} — resumable: {e}",
                )

        # Regenerate stale lockfiles so the PR is born with a correct lock.
        _regen_lockfiles(repo_dir, target, ticket.id)

        try:
            git_ops.push(repo_dir, branch, remote_url, token)
        except subprocess.CalledProcessError as e:
            return None, Outcome(
                State.BLOCKED,
                f"push failed for {repo_label} — resumable: {(e.stderr or '')[:300]}",
            )

        title = f"mill: {ticket.title} ({ticket.id})"
        ws = ctx.service.workspace(ticket)
        spec = ws.read_description()

        # Generate a structured PR body from the diff when enabled.
        # Fall back to the raw spec on any failure.
        body: str
        if s.pr_summary_enabled:
            try:
                diff = git_ops.diff_base(repo_dir, target)
                if not diff.strip():
                    body = spec[:8000]
                else:
                    body = generate_pr_description(diff, spec, s)
            except Exception:
                log.exception(
                    "%s: PR summary diff/summary failed; falling back to raw spec",
                    ticket.id,
                )
                body = spec[:8000]
        else:
            body = spec[:8000]

        body += f"\n\n---\nAutomated by robotsix-mill · ticket `{ticket.id}`"
        if s.pr_summary_enabled:
            body += (
                f"\n\n<details>\n<summary>Original ticket spec</summary>\n\n"
                f"{spec}\n</details>"
            )

        try:
            forge = get_forge(s, repo_config=repo_config)
            if cross_repo_target is not None:
                # Cross-repo: open the PR fork→upstream. Pass the fork's
                # ``owner/repo`` as the head so GitHub forms
                # ``head=<fork-owner>:<branch>``.
                fork_owner, fork_repo = _parse_owner_repo(
                    cross_repo_target.fork_remote_url
                )
                url = forge.open_merge_request(
                    source_branch=branch,
                    title=title,
                    body=body,
                    head_repo=f"{fork_owner}/{fork_repo}",
                )
            else:
                url = forge.open_merge_request(
                    source_branch=branch, title=title, body=body
                )
        except Exception as e:  # noqa: BLE001 — resumable, don't lose branch
            log.exception("%s: open PR failed for %s", ticket.id, repo_label)
            return None, Outcome(
                State.BLOCKED,
                f"open PR failed for {repo_label} — resumable: {e}",
            )

        return url, None
