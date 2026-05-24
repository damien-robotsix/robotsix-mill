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

from ..agents.documenting import DocResult
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
                State.DELIVERABLE,
                "empty diff (no documentation needed)",
            )

        spec = ws.read_description()

        # --- Documentation agent ---
        try:
            doc_result = self._run_doc_agent(
                settings=s,
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
                State.DELIVERABLE,
                "doc agent failed (non-blocking)",
            )

        next_state = State.DELIVERABLE

        if doc_result.user_facing:
            try:
                if git_ops.has_changes(repo_dir):
                    git_ops.commit_all(
                        repo_dir,
                        f"mill(docs): {ticket.title} ({ticket.id})",
                    )
            except Exception:
                log.warning(
                    "%s: doc commit failed — passing through",
                    ticket.id,
                    exc_info=True,
                )
            return Outcome(next_state, doc_result.summary)

        return Outcome(next_state, "no user-facing changes (internal-only)")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_doc_agent(
        self,
        *,
        settings,
        repo_dir,
        diff: str,
        spec: str,
        model_name: str | None = None,
    ) -> DocResult:
        """Run the documentation agent to classify the diff and update docs.

        Returns a ``DocResult`` with ``user_facing`` (bool) and ``summary``
        (str describing what was updated or that no changes were needed).
        """
        from ..agents.documenting import run_doc_agent

        return run_doc_agent(
            settings=settings,
            repo_dir=repo_dir,
            diff=diff,
            spec=spec,
            model_name=model_name,
        )
