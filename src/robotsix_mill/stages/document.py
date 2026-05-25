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
from pathlib import Path

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

        # --- Phase 1: cheap classifier gate ---
        try:
            classifier_result = self._run_doc_classifier(
                settings=s, diff=diff, spec=spec,
            )
            ctx.service.add_comment(
                ticket.id,
                f"classifier: {classifier_result.classification}",
                author="doc_classifier",
            )
            if not classifier_result.user_facing:
                log.info(
                    "%s: classifier → internal-only — skipping full doc agent",
                    ticket.id,
                )
                return Outcome(
                    State.DELIVERABLE,
                    f"no user-facing changes ({classifier_result.classification})",
                )
        except Exception:
            log.warning(
                "%s: doc classifier failed — falling through to full doc agent",
                ticket.id,
                exc_info=True,
            )

        # --- Phase 2: full documentation agent ---
        try:
            doc_result = self._run_doc_agent(
                settings=s,
                repo_dir=repo_dir,
                diff=diff,
                spec=spec,
                extra_roots=None,
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
        extra_roots: list[Path] | None = None,
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
            extra_roots=extra_roots,
        )

    def _run_doc_classifier(
        self,
        *,
        settings,
        diff: str,
        spec: str,
    ):
        """Run the cheap, tool-free doc classifier gate.

        Returns a ``DocClassifierResult`` with ``user_facing`` and
        ``classification`` fields.  No tools, no file-system access —
        pure diff-and-spec inspection.
        """
        from ..agents.documenting import run_doc_classifier, DocClassifierResult

        return run_doc_classifier(
            settings=settings,
            diff=diff,
            spec=spec,
        )
