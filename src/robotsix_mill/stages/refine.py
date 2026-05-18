"""Refine stage: raw DRAFT -> actionable READY ticket.

Rewrites the file-canonical ``description.md`` into a precise spec the
implement agent can act on unattended; the original draft is kept as an
artifact for traceability. Empty draft or missing OpenRouter key ->
BLOCKED with a clear note (not a crash).

When ``require_approval`` is true (the default), the refined ticket
enters ``awaiting_approval`` instead of ``ready`` — a human must approve
before the implement stage picks it up.
"""

from __future__ import annotations

import logging
import subprocess

from ..agents import refining
from ..core.models import Ticket
from ..core.states import State
from ..vcs import git_ops
from .base import Outcome, Stage, StageContext

log = logging.getLogger("robotsix_mill.stages.refine")


class RefineStage(Stage):
    name = "refine"
    input_state = State.DRAFT

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        ws = ctx.service.workspace(ticket)
        draft = ws.read_description().strip()
        if not draft:
            return Outcome(State.BLOCKED, "empty draft — nothing to refine")

        # Ground the spec in the ACTUAL repo: clone it locally so the
        # refine agent uses explore/read_file instead of web-fetching
        # the project's own files. Best-effort — a clone failure (or no
        # forge configured) just falls back to draft-only refinement.
        s = ctx.settings
        repo_dir = None
        if s.forge_remote_url:
            cand = ws.dir / "repo"
            if (cand / ".git").exists():
                repo_dir = cand  # idempotent: reuse an existing clone
            else:
                try:
                    git_ops.clone(
                        s.forge_remote_url, cand,
                        s.forge_target_branch, s.forge_token,
                    )
                    repo_dir = cand
                except subprocess.CalledProcessError as e:
                    log.warning(
                        "%s: refine clone failed, draft-only: %s",
                        ticket.id, (e.stderr or "")[:200],
                    )

        try:
            spec = refining.run_refine_agent(
                settings=s, title=ticket.title, draft=draft,
                repo_dir=repo_dir,
            )
        except RuntimeError as e:  # e.g. OPENROUTER_API_KEY not set
            return Outcome(State.BLOCKED, str(e))

        if not spec.strip():
            return Outcome(State.BLOCKED, "refiner produced an empty spec")

        # preserve the raw draft, then make the refined spec canonical
        (ws.artifacts_dir / "draft-original.md").write_text(
            draft, encoding="utf-8"
        )
        new_hash = ws.write_description(spec)
        ctx.service.set_content_hash(ticket.id, new_hash)

        next_state = (
            State.AWAITING_APPROVAL if ctx.settings.require_approval
            else State.READY
        )
        return Outcome(next_state, "refined")
