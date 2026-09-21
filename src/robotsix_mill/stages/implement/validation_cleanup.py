"""Deterministic artifact-cleanup helpers for the implement scope guardrail.

Schema-based, LLM-free filtering applied by ``_run_scope_guardrail``
*before* the scope-triage LLM call: binary-artifact cleanup, vendored-dep
directory exclusion, and standard config-file auto-revert.  These are pure
file-system operations with no model dependency, so they live apart from
the orchestration in :mod:`.validation`.

The three operations are classmethods on :class:`ValidationCleanupMixin`,
which :class:`~.validation.ValidationMixin` inherits — so the orchestrator
keeps calling them via ``cls._clean_binary_artifacts(...)`` and callers
that reference them off the stage class continue to resolve.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ...core.models import Ticket
from ..base import StageContext
from ._shared import (
    _is_binary_artifact,
    _ScopeGuardrailResult,
    _vendored_dep_roots,
    log,
)

# Standard repo scaffolding files that the agent may regenerate
# inadvertently (driven by AGENT.md conventions or system-prompt rules
# about .pre-commit-config.yaml, mkdocs.yml, etc.).  When these files
# appear out-of-scope after an implement pass, auto-revert them to
# origin/<target> rather than blocking the ticket or consuming LLM
# tokens on scope-triage.
_STANDARD_CONFIG_FILES: frozenset[str] = frozenset(
    {
        ".pre-commit-config.yaml",
        "docker-compose.yml",
        "mkdocs.yml",
    }
)


def _spec_names_path(spec: str, path: str) -> bool:
    """True when the ticket *spec* mentions *path* by name.

    A plain substring test is the right strength here: these paths are
    distinctive filenames, and the only decision it drives is whether to
    skip an auto-revert and let the normal scope-triage LLM judge the file
    instead.  A false positive therefore costs one scope-triage call; a
    false negative silently deletes the ticket's deliverable.
    """
    return bool(spec) and path in spec


class ValidationCleanupMixin:
    """Deterministic pre-triage artifact cleanup for the scope guardrail."""

    @classmethod
    def _clean_binary_artifacts(
        cls,
        ctx: StageContext,
        ticket: Ticket,
        repo_dir: Path,
        target: str,
        out_of_scope: list[str],
        file_map: set[str] | None,
        current_feedback: str | None,
    ) -> tuple[list[str], _ScopeGuardrailResult | None]:
        """Split out binary artifacts, clean them, and return text-only files.

        Returns ``(text_out_of_scope, None)`` when text files remain for
        further processing, or ``(text_out_of_scope, skip_result)`` when
        ALL out-of-scope files were binary artifacts (caller must return
        ``skip_result`` immediately).
        """
        binary_artifacts: list[str] = []
        text_out_of_scope: list[str] = []
        for f in out_of_scope:
            (
                binary_artifacts
                if _is_binary_artifact(repo_dir, f, target)
                else text_out_of_scope
            ).append(f)

        if binary_artifacts:
            cleaned: list[str] = []
            for path in binary_artifacts:
                # Restore tracked version first (no-op for untracked).
                try:
                    subprocess.run(
                        ["git", "-C", str(repo_dir), "checkout", "--", path],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                except subprocess.CalledProcessError:
                    log.debug(
                        "_clean_binary_artifacts: git checkout failed for %s "
                        "— ignoring git failure",
                        path,
                        exc_info=True,
                    )
                # If the file still exists on disk, it was untracked
                # — remove it.
                file_path = repo_dir / path
                try:
                    if file_path.exists():
                        file_path.unlink()
                except OSError:
                    log.warning(
                        "%s: failed to unlink binary artifact: %s",
                        ticket.id,
                        path,
                        exc_info=True,
                    )
                log.warning(
                    "%s: auto-cleaned binary artifact: %s",
                    ticket.id,
                    path,
                )
                cleaned.append(path)

            ctx.service.add_step_event(
                ticket.id,
                "scope-triage auto-REJECT (binary artifacts): removed "
                + ", ".join(f"`{f}`" for f in cleaned)
                + " — runtime artifacts, not real work",
            )

        if not text_out_of_scope:
            log.info(
                "%s: all out-of-scope files were binary artifacts — "
                "skipping scope-triage LLM call",
                ticket.id,
            )
            return text_out_of_scope, _ScopeGuardrailResult(
                action="skip_iteration",
                file_map=file_map,
                feedback=current_feedback,
            )

        return text_out_of_scope, None

    @classmethod
    def _filter_vendored_deps(
        cls,
        ctx: StageContext,
        ticket: Ticket,
        repo_dir: Path,
        target: str,
        out_of_scope: list[str],
        file_map: set[str] | None,
        current_feedback: str | None,
    ) -> tuple[list[str], _ScopeGuardrailResult | None]:
        """Exclude vendored-dependency install directories by content signature.

        Pip/uv/npm vendored-dependency install dirs (``.pip-packages/``,
        ``local-deps/``, ``.deps/``, …) are UNtracked, have no durable
        name, and repeatedly flood the out-of-scope set with
        ``*.dist-info/METADATA``-style files. Detect them by CONTENT
        SIGNATURE, gate on UNtracked status, and log every auto-ignored
        dir non-silently.

        Returns ``(filtered, None)`` when text files remain for further
        processing, or ``(filtered, skip_result)`` when ALL out-of-scope
        files were in vendored-dep dirs.
        """
        vendored_roots = _vendored_dep_roots(repo_dir, out_of_scope, target)
        if not vendored_roots:
            return out_of_scope, None

        excluded: dict[str, int] = {}
        for root in vendored_roots:
            excluded[root] = sum(1 for f in out_of_scope if f.startswith(root + "/"))
        filtered = [f for f in out_of_scope if f.split("/", 1)[0] not in vendored_roots]
        for root, count in sorted(excluded.items()):
            msg = (
                f"scope-triage auto-ignored vendored-dep dir by content "
                f"signature: `{root}/` ({count} files) — untracked "
                f"install target, not scope creep"
            )
            log.info("%s: %s", ticket.id, msg)
            ctx.service.add_step_event(ticket.id, msg)
        if not filtered:
            log.info(
                "%s: all out-of-scope files were in vendored-dep dirs — "
                "skipping scope-triage LLM call",
                ticket.id,
            )
            return filtered, _ScopeGuardrailResult(
                action="skip_iteration",
                file_map=file_map,
                feedback=current_feedback,
            )
        return filtered, None

    @classmethod
    def _revert_standard_configs(
        cls,
        ctx: StageContext,
        ticket: Ticket,
        repo_dir: Path,
        target: str,
        out_of_scope: list[str],
        file_map: set[str] | None,
        current_feedback: str | None,
        spec: str = "",
    ) -> tuple[list[str], _ScopeGuardrailResult | None]:
        """Auto-revert known standard config files when they appear out-of-scope.

        The implement agent occasionally regenerates repo scaffolding
        files (``.pre-commit-config.yaml``, ``docker-compose.yml``,
        ``mkdocs.yml``) that are inherited from the default branch and
        not part of the ticket's scope.  These are driven by AGENT.md
        conventions or system-prompt rules about those files — not by
        deliberate ticket work.

        A file the ticket's own *spec* names is exempt: it is the
        deliverable, not scaffolding drift.  Without that carve-out a
        ticket whose entire job is to edit one of these files can never
        land — implement writes it, this revert undoes it, review reports
        it as never written, and the pass repeats until the cycle ceiling
        blocks the ticket.  Observed live on two tickets whose sole
        deliverable was ``docker-compose.yml`` (hexarchy e5c4, blocked
        across five attempts from 2026-08-10; file-hub de52).  ``file_map``
        cannot serve here: *out_of_scope* is already ``changed - file_map``,
        so its membership test can never fire.

        Reverts tracked files to ``origin/<target>`` and removes
        untracked copies, logging each reverted file.  Returns
        ``(remaining, None)`` when text files remain for further
        processing, or ``(remaining, skip_result)`` when ALL
        out-of-scope files were standard config files.
        """
        standard_hits: list[str] = []
        remaining: list[str] = []
        for f in out_of_scope:
            if f in _STANDARD_CONFIG_FILES and not _spec_names_path(spec, f):
                standard_hits.append(f)
            else:
                remaining.append(f)

        if standard_hits:
            for path in standard_hits:
                try:
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            str(repo_dir),
                            "checkout",
                            f"origin/{target}",
                            "--",
                            path,
                        ],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                except subprocess.CalledProcessError:
                    log.debug(
                        "_revert_standard_configs: git checkout failed for %s "
                        "— file may be untracked; unlinking",
                        path,
                        exc_info=True,
                    )
                    file_path = repo_dir / path
                    try:
                        if file_path.exists():
                            file_path.unlink()
                    except OSError:
                        log.warning(
                            "%s: failed to unlink standard config file: %s",
                            ticket.id,
                            path,
                            exc_info=True,
                        )
                log.warning(
                    "%s: auto-reverted standard config file (out of scope): %s",
                    ticket.id,
                    path,
                )

            ctx.service.add_step_event(
                ticket.id,
                "scope-triage auto-REVERT (standard config): reverted "
                + ", ".join(f"`{f}`" for f in standard_hits)
                + " — repo scaffolding, not ticket work",
            )

        if not remaining:
            log.info(
                "%s: all out-of-scope files were standard config files — "
                "skipping scope-triage LLM call",
                ticket.id,
            )
            return remaining, _ScopeGuardrailResult(
                action="skip_iteration",
                file_map=file_map,
                feedback=current_feedback,
            )

        return remaining, None
