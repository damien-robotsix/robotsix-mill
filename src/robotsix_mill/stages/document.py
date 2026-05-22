"""Document stage: DOCUMENTING -> CODE_REVIEW | DELIVERABLE.

Inspects the implementation diff and, when the change is user-facing,
updates the relevant documentation files. For internal-only changes
(pure refactors, bug fixes with no doc impact) this stage is a no-op
and passes straight through.

The doc agent runs with warn-and-pass semantics: if it raises an
exception the ticket still progresses — losing a finished
implementation over a doc-update hiccup is the wrong trade.
"""

from __future__ import annotations

import logging

from ..core.models import Ticket
from ..core.states import State
from ..vcs import git_ops
from .base import Outcome, Stage, StageContext

log = logging.getLogger("robotsix_mill.stages.document")


class DocumentStage(Stage):
    name = "document"
    input_state = State.DOCUMENTING
    traced = True

    def run(self, ticket: Ticket, ctx: StageContext) -> Outcome:
        s = ctx.settings

        ws = ctx.service.workspace(ticket)
        repo_dir = ws.dir / "repo"

        # Guard: missing clone → BLOCKED (resumable: re-run implement)
        if not (repo_dir / ".git").exists():
            return Outcome(
                State.BLOCKED,
                "no repository clone (re-run implement)",
            )

        target_branch = s.forge_target_branch

        # Compute diff of all commits on the current branch vs origin/<target>.
        try:
            diff = git_ops.diff_base(repo_dir, target_branch)
        except Exception as e:
            return Outcome(
                State.BLOCKED,
                f"failed to compute diff: {e}",
            )

        # Empty diff → nothing to document, pass through.
        if not diff.strip():
            log.info("%s: empty diff — no documentation needed", ticket.id)
            return Outcome(
                State.CODE_REVIEW if s.review_enabled else State.DELIVERABLE,
                "empty diff (no documentation needed)",
            )

        spec = ws.read_description()

        # --- Documentation agent ---
        # Stub: classify change as user-facing vs internal, and edit docs
        # when needed.  The real agent will be wired in a follow-up.
        try:
            result_note = self._run_doc_agent(
                settings=s,
                ticket=ticket,
                repo_dir=repo_dir,
                diff=diff,
                spec=spec,
            )
        except Exception:
            log.warning(
                "%s: doc agent failed — passing through",
                ticket.id,
                exc_info=True,
            )
            return Outcome(
                State.CODE_REVIEW if s.review_enabled else State.DELIVERABLE,
                "doc agent failed (non-blocking)",
            )

        return Outcome(
            State.CODE_REVIEW if s.review_enabled else State.DELIVERABLE,
            result_note,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_doc_agent(
        self,
        *,
        settings,
        ticket: Ticket,
        repo_dir,
        diff: str,
        spec: str,
    ) -> str:
        """Stub documentation agent.

        In the real implementation this will:
        1. Classify the change as user-facing or internal.
        2. Identify which docs need updating (README.md, docs/*, AGENT.md,
           or inline docstrings).
        3. Apply the edits directly in the workspace.
        4. Commit them.

        For now the stub treats every change as internal (no-op).
        """
        del settings, ticket, repo_dir, diff, spec
        return "no user-facing changes (internal-only)"
