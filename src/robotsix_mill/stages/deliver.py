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
"""

from __future__ import annotations

import logging
import subprocess

from ..agents.base import build_agent
from ..agents.retry import run_agent
from ..core.models import Ticket
from ..core.states import State
from ..forge import get_forge
from ..forge.auth import _resolve_remote_url, github_token
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
            model_name=settings.pr_summary_model,
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
            settings=settings,
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


class DeliverStage(Stage):
    """Push the implemented branch to the remote forge for review."""

    name = "deliver"
    input_state = State.DELIVERABLE
    traced = False

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        """Push the implemented branch to the remote forge and open (or update) a pull/merge request, transitioning the ticket toward review."""
        s = ctx.settings
        remote_url = _resolve_remote_url(s, ctx.repo_config)
        if s.forge_kind == "none":
            return Outcome(State.BLOCKED, "FORGE_KIND not configured")
        if not remote_url:
            return Outcome(State.BLOCKED, "FORGE_REMOTE_URL not configured")
        try:
            token = github_token(
                s, repo_config=ctx.repo_config
            )  # PAT or minted App installation token
        except RuntimeError as e:
            return Outcome(State.BLOCKED, f"forge auth not configured: {e}")

        ws = ctx.service.workspace(ticket)
        repo_dir = ws.dir / "repo"
        branch = ticket.branch or f"{s.branch_prefix}{ticket.id}"
        if not (repo_dir / ".git").exists() or not git_ops.branch_exists(
            repo_dir, branch
        ):
            return Outcome(
                State.BLOCKED,
                "no implemented branch to deliver (re-run implement)",
            )

        try:
            git_ops.push(repo_dir, branch, remote_url, token)
        except subprocess.CalledProcessError as e:
            return Outcome(
                State.BLOCKED,
                f"push failed — resumable: {(e.stderr or '')[:300]}",
            )

        # Guard: skip PR creation when the branch has no new commits
        # relative to origin/main. This avoids a 422 "No commits
        # between main and branch" from GitHub when the implement agent
        # produced no net diff.
        #
        # Route to DONE rather than BLOCKED: the implement stage's
        # own ``no_change_needed`` gate (and its silent-no-change
        # BLOCK on fresh runs) has already filtered out the cases
        # where there *should* have been a diff. By the time we
        # reach deliver with an empty branch, the conclusion is
        # "the spec is already satisfied; there is nothing to ship."
        # Mirrors refine's ``no_change_needed`` bypass — same shape,
        # just one stage later.
        if not git_ops.branch_is_ahead_of_main(repo_dir):
            return Outcome(
                State.DONE,
                "no change needed — branch contains no new commits vs "
                f"{s.forge_target_branch}; the spec was already satisfied "
                "by the current codebase",
            )

        title = f"mill: {ticket.title} ({ticket.id})"
        spec = ws.read_description()

        # Generate a structured PR body from the diff when enabled.
        # Fall back to the raw spec on any failure.
        body: str
        if s.pr_summary_enabled:
            try:
                diff = git_ops.diff_base(repo_dir, s.forge_target_branch)
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
            url = get_forge(s, repo_config=ctx.repo_config).open_merge_request(
                source_branch=branch, title=title, body=body
            )
        except Exception as e:  # noqa: BLE001 — resumable, don't lose branch
            log.exception("%s: open PR failed", ticket.id)
            return Outcome(State.BLOCKED, f"open PR failed — resumable: {e}")

        (ws.artifacts_dir / "deliver.md").write_text(
            f"# Deliver (passed)\nbranch: {branch}\nPR: {url}\n",
            encoding="utf-8",
        )
        log.info("%s: delivered → %s", ticket.id, url)
        # PR opened — gates not yet verified; merge stage will poll
        return Outcome(State.IMPLEMENT_COMPLETE, f"PR: {url}")
