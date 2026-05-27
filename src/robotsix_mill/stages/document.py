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
import re
from pathlib import Path

from ..agents.documenting import DocClassifierResult, DocResult
from ..core.models import Ticket
from ..core.states import State
from ..forge.auth import _resolve_remote_url, github_token
from ..vcs import git_ops
from .base import Outcome, Stage, StageContext

log = logging.getLogger("robotsix_mill.stages.document")


def _paths_from_diff(diff: str) -> list[str]:
    """Extract modified file paths from a unified git diff.

    Mirrors ``stages.review._paths_from_diff`` — kept as a local copy
    (instead of an import) to avoid a stage-to-stage dependency for a
    single regex; if a third stage needs it, lift to ``vcs.git_ops``.
    """
    seen: set[str] = set()
    out: list[str] = []
    for m in re.finditer(r"^\+\+\+ b/(.+)$", diff, re.MULTILINE):
        path = m.group(1).strip()
        if path and path != "/dev/null" and path not in seen:
            seen.add(path)
            out.append(path)
    return out


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

        # Mint a fresh forge token for the fetch — the clone's baked-in
        # GitHub App installation token expires ~1h after clone time,
        # so a stale ``origin`` URL would 401 with exit 128. Same fix
        # applied earlier to the review stage; document runs right
        # after review on the same long-lived clone.
        remote_url = _resolve_remote_url(s, ctx.repo_config)
        try:
            token = github_token(s, repo_config=ctx.repo_config)
        except RuntimeError:
            token = None

        # Compute diff of all commits on the current branch vs origin/<target>.
        try:
            diff = git_ops.diff_base(
                repo_dir, target_branch,
                remote_url=remote_url, token=token,
            )
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
        # A single cheap LLM call decides whether the diff is user-facing.
        # Internal-only diffs skip the full (expensive) doc agent entirely.
        # Failure is non-blocking — we fall through to the full agent.
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
                    "%s: classifier says internal-only — skipping doc agent",
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

        # Pre-load the modified files (parsed from the diff) plus
        # whichever top-level docs actually exist (README.md, AGENT.md)
        # so the doc agent doesn't have to read each file via a
        # separate round-trip. Same pattern review uses.
        modified_paths = _paths_from_diff(diff)
        preload_paths: list[str] = list(modified_paths)
        for doc_name in ("README.md", "AGENT.md"):
            if doc_name not in preload_paths and (repo_dir / doc_name).exists():
                preload_paths.append(doc_name)

        # --- Phase 2: full documentation agent ---
        try:
            doc_result = self._run_doc_agent(
                settings=s,
                repo_dir=repo_dir,
                diff=diff,
                spec=spec,
                extra_roots=None,
                board_id=ctx.repo_config.board_id if ctx.repo_config else "",
                reference_files=preload_paths or None,
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
        board_id: str = "",
        reference_files: list[str] | None = None,
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
            board_id=board_id,
            reference_files=reference_files,
        )

    def _run_doc_classifier(
        self,
        *,
        settings,
        diff: str,
        spec: str,
    ) -> DocClassifierResult:
        """Run the cheap classifier gate to decide whether the diff is
        user-facing.

        Returns a ``DocClassifierResult`` with ``user_facing`` (bool) and
        ``classification`` (human-readable one-liner).
        """
        from ..agents.documenting import run_doc_classifier

        return run_doc_classifier(
            settings=settings,
            diff=diff,
            spec=spec,
        )
