"""Special-case edit handlers mixin for the implement stage.

Extracted from ``implementation_logic.py`` — handles rename-only,
spec-exact-code, and verification-of-changes patterns that are
separable from the core agent invocation, test evaluation, and
smoke gate concerns.
"""

from __future__ import annotations

import difflib
import re as _re
import subprocess as sp
from pathlib import Path

from ...config import ConfigError, Settings, get_repo_config, target_branch_for
from ...core.models import Ticket
from ...core.states import State
from ...vcs import git_ops
from .. import short_circuit_verify
from ..base import Outcome, StageContext
from ._base import _ImplementStageBase
from ._shared import (
    _ImplementContext,
    _SinglePassResult,
    _parse_spec_code_blocks,
    log,
)


class _ImplementationEditingMixin(_ImplementStageBase):
    """Special-case edit handlers: rename-only, spec-exact, insertion-point logic."""

    @classmethod
    def _verify_repo_changes(
        cls,
        ctx: StageContext,
        ticket: Ticket,
        repo_dir: Path,
        branch: str,
        settings: Settings,
        summary: str,
        ref_files: list[str] | None,
        new_msgs: bytes | None,
        new_ic: _ImplementContext,
        ic: _ImplementContext,
        target: str,
        extra_roots: list[Path] | None,
        resuming: bool,
        attempt: int,
        max_iters: int,
    ) -> _SinglePassResult | None:
        """Verify that claimed file edits actually landed in the diff.

        Two guards:
        1. **Per-claimed-file check** — when the summary names files
           that are absent from the net diff, surface a contradiction
           before handing off to review.
        2. **Zero-tool-call resume guard** — when resuming and no tool
           calls were issued with an empty diff, surface a distinct
           error instead of routing to CODE_REVIEW with no work.

        Returns a ``_SinglePassResult`` when a guard fires (caller must
        return it); ``None`` when all checks pass.
        """
        changed = git_ops.introduced_files(repo_dir, target)
        if extra_roots:
            for repo_path in extra_roots:
                if repo_path == repo_dir:
                    continue
                try:
                    rc = get_repo_config(repo_path.name)
                except ConfigError:
                    rc = None
                repo_target = target_branch_for(settings, rc)
                changed = list(
                    set(changed) | set(git_ops.introduced_files(repo_path, repo_target))
                )
        missing = short_circuit_verify.detect_missing_claimed_files(
            changed_files=changed,
            new_messages=new_msgs,
            summary=summary,
        )
        if missing:
            file_list = ", ".join(missing)
            diag = (
                "[Diagnostic] Your summary / thread-reply claims edits to "
                f"the following file(s) — {file_list} — but they are ABSENT "
                "from the net diff vs "
                f"origin/{target}. An edit-tool-call "
                "targeted each of them and your summary names them as fixed, "
                "yet the working tree does not contain those changes (edits "
                "reverted, written outside the clone, or never applied). "
                "Before completing, actually apply those edits so they land "
                "in the diff — OR correct your summary so it does not claim "
                "edits you did not make. Do not hand un-landed claims to "
                "review."
            )
            if attempt < max_iters:
                new_ic.feedback = diag
                return _SinglePassResult(
                    next_action="retry",
                    feedback=diag,
                    ic=new_ic,
                    new_msgs=new_msgs,
                )
            cls._finalize(
                ctx,
                ticket,
                repo_dir,
                branch,
                diag,
                ok=False,
                reference_files=ref_files,
                extra_roots=extra_roots,
            )
            return _SinglePassResult(
                next_action="return",
                outcome=Outcome(
                    State.BLOCKED,
                    "edit-claim contradiction (claimed files absent from diff)",
                ),
            )

        # Zero-tool-call guard for resumed passes.
        if (
            resuming
            and not changed
            and not cls._any_repo_has_changes(
                repo_dir, extra_roots, target, settings=settings
            )
        ):
            progress = short_circuit_verify.analyze_pass_progress(new_msgs)
            if progress["total"] == 0:
                diag = (
                    "zero tool calls on resume — the implement agent "
                    "completed this pass without issuing a single tool "
                    "call and produced no file changes.  The previous "
                    "implement session left no surviving diff either, so "
                    "there is nothing to hand to code review.  This "
                    "indicates a prompt/context assembly failure, "
                    "workspace inaccessibility, or a spec the agent "
                    "cannot act on."
                )
                cls._finalize(
                    ctx,
                    ticket,
                    repo_dir,
                    branch,
                    diag,
                    ok=False,
                    reference_files=ref_files,
                    extra_roots=extra_roots,
                )
                return _SinglePassResult(
                    next_action="return",
                    outcome=Outcome(
                        State.BLOCKED,
                        f"zero tool calls on resume — "
                        f"{diag[:200]}"
                        + ("… (see implement.md)" if len(diag) > 200 else ""),
                    ),
                )
        return None

    @classmethod
    def _handle_rename_only_change(
        cls,
        ctx: StageContext,
        ticket: Ticket,
        repo_dir: Path,
        branch: str,
        settings: Settings,
        ic: _ImplementContext,
        target: str,
        extra_roots: list[Path] | None,
    ) -> _SinglePassResult:
        """Handle a rename-only change deterministically — no LLM invocation.

        Collects the rename list for the summary, persists artifacts,
        runs the scope guardrail, and routes to test evaluation (which
        will skip via :func:`_should_skip_test_gate`).
        """
        # Collect renamed files for the summary.
        rename_out = sp.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "diff",
                "--diff-filter=R",
                "--name-only",
                f"origin/{target}",
            ],
            capture_output=True,
            text=True,
        )
        renamed: list[str] = (
            rename_out.stdout.strip().splitlines() if rename_out.returncode == 0 else []
        )

        # Collect all changed files for reference_files.
        all_out = sp.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "diff",
                "--name-only",
                f"origin/{target}",
            ],
            capture_output=True,
            text=True,
        )
        all_changed: list[str] = (
            all_out.stdout.strip().splitlines() if all_out.returncode == 0 else []
        )

        # Build a deterministic summary.
        renamed_preview = ", ".join(renamed[:5])
        if len(renamed) > 5:
            renamed_preview += f" (+{len(renamed) - 5} more)"
        summary = f"rename-only change: {len(renamed)} file(s) renamed" + (
            f" — {renamed_preview}" if renamed_preview else ""
        )

        ws = ctx.service.workspace(ticket)
        memory_board_id = cls._memory_board_id(ctx, ticket)

        # Persist artifacts (no memory update — no agent ran).
        ref_files = all_changed
        cls._persist_pass_artifacts(
            ws,
            ticket,
            ic,
            summary,
            ref_files,
            "",
            settings,
            memory_board_id,
        )

        # Run scope guardrail.
        guardrail = cls._run_scope_guardrail(
            ctx,
            ticket,
            repo_dir,
            branch,
            summary,
            ref_files,
            ic.file_map,
            settings,
            ic.spec,
            ic.feedback,
        )
        if guardrail.action == "return":
            return _SinglePassResult(next_action="return", outcome=guardrail.outcome)

        new_file_map = (
            guardrail.file_map if guardrail.file_map is not None else ic.file_map
        )
        new_feedback = (
            guardrail.feedback
            if guardrail.action in ("continue", "skip_iteration")
            else ic.feedback
        )
        new_ic = _ImplementContext(
            spec=ic.spec,
            memory_text=ic.memory_text,
            reference_files=[{"path": p} for p in ref_files],
            file_map=new_file_map,
            feedback=new_feedback,
            previous_attempt_summary=summary,
            open_thread_ids=ic.open_thread_ids,
        )
        if guardrail.action == "continue":
            return _SinglePassResult(next_action="retry", feedback=None, ic=new_ic)

        # Route to test evaluation (which will skip via _should_skip_test_gate).
        return cls._evaluate_test_results(  # type: ignore[no-any-return]
            ctx,
            ticket,
            repo_dir,
            branch,
            settings,
            ic,
            new_ic,
            summary,
            ref_files,
            None,  # new_msgs
            False,  # no_change_needed
            "",  # no_change_rationale
            False,  # resuming
            1,  # attempt
            max(1, settings.max_fix_iterations),  # max_iters
            extra_roots,
        )

    @classmethod
    def _handle_spec_exact_edits(
        cls,
        ctx: StageContext,
        ticket: Ticket,
        repo_dir: Path,
        branch: str,
        settings: Settings,
        ic: _ImplementContext,
        target: str,
        extra_roots: list[Path] | None,
    ) -> _SinglePassResult:
        """Handle a spec-exact-code ticket deterministically — no LLM invocation.

        Parses fenced code blocks annotated with file paths from the
        ticket description, applies them as edits (unified-diff or
        context-aware replacement), then runs the scope guardrail and
        routes to test evaluation.
        """
        blocks = _parse_spec_code_blocks(ic.spec)

        applied: list[str] = []
        failed: list[str] = []
        changed_files: set[str] = set()

        for file_path, _info, code in blocks:
            target_file = repo_dir / file_path
            if not target_file.is_file():
                failed.append(f"{file_path}: file not found")
                continue

            original = target_file.read_text()

            # --- Strategy 1: unified diff ---------------------------------
            if (
                code.startswith("--- ")
                or code.startswith("+++ ")
                or code.startswith("@@")
            ):
                try:
                    result = sp.run(
                        ["patch", "--batch", "-p0", "-o", "-", str(target_file)],
                        input=code,
                        capture_output=True,
                        text=True,
                        cwd=str(repo_dir),
                        timeout=10,
                    )
                    if result.returncode == 0 and result.stdout:
                        target_file.write_text(result.stdout)
                        applied.append(file_path)
                        changed_files.add(file_path)
                        continue
                except Exception as exc:
                    log.debug(
                        "Spec-exact: unified diff failed for %s: %s", file_path, exc
                    )

            # --- Strategy 2: context-aware replacement --------------------
            # Split both into lines and find the longest matching region.
            file_lines = original.splitlines(keepends=True)
            code_lines = code.splitlines(keepends=True)
            code_stripped = [line.rstrip("\n\r") for line in code_lines]

            sm = difflib.SequenceMatcher(
                None,
                [line.rstrip("\n\r") for line in file_lines],
                code_stripped,
            )
            match = sm.find_longest_match(0, len(file_lines), 0, len(code_stripped))

            # Require at least 2 matching lines (or 1 if the code block
            # is only 1 line) to consider it a context match.
            min_match = min(2, len(code_stripped))
            if match.size >= min_match:
                # Replace the matched region with the full code block.
                new_lines = (
                    file_lines[: match.a]
                    + code_lines
                    + file_lines[match.a + match.size :]
                )
                new_content = "".join(new_lines)
                if new_content != original:
                    target_file.write_text(new_content)
                    applied.append(file_path)
                    changed_files.add(file_path)
                    continue

            # --- Strategy 3: insertion via context hints ------------------
            # Look for insertion-point hints in the lines preceding the
            # code block in the spec.
            insertion_point = cls._find_insertion_point(ic.spec, code, file_lines)
            if insertion_point is not None:
                new_lines = (
                    file_lines[:insertion_point]
                    + code_lines
                    + file_lines[insertion_point:]
                )
                new_content = "".join(new_lines)
                target_file.write_text(new_content)
                applied.append(file_path)
                changed_files.add(file_path)
                continue

            failed.append(f"{file_path}: could not determine edit location")

        if not applied:
            # Nothing was applied — fall through to LLM path via retry.
            # Include a sentinel ``_ImplementContext`` so
            # ``_select_agent_level`` detects the prior failed attempt
            # and returns ``None`` (→ LLM path) instead of ``-1``,
            # breaking what would otherwise be an infinite retry loop
            # (spec is static, so ``_is_spec_exact_edits`` keeps
            # returning ``True`` across retries).
            log.warning(
                "Spec-exact bypass: no edits applied (%d block(s) failed: %s)",
                len(failed),
                ", ".join(failed[:5]),
            )
            fail_summary = (
                f"spec-exact bypass: failed — {len(failed)} block(s) unapplied"
            )
            return _SinglePassResult(
                next_action="retry",
                feedback=(
                    "Spec-exact bypass: could not apply any edits. "
                    + f"Failed: {', '.join(failed[:3])}"
                ),
                ic=_ImplementContext(
                    spec=ic.spec,
                    memory_text=ic.memory_text,
                    reference_files=ic.reference_files,
                    file_map=ic.file_map,
                    feedback=ic.feedback,
                    previous_attempt_summary=fail_summary,
                    open_thread_ids=ic.open_thread_ids,
                ),
            )

        # Build a deterministic summary.
        applied_preview = ", ".join(applied[:5])
        if len(applied) > 5:
            applied_preview += f" (+{len(applied) - 5} more)"
        summary = f"spec-exact edit: {len(applied)} file(s) changed — {applied_preview}"

        if failed:
            summary += f" ({len(failed)} block(s) skipped)"

        ws = ctx.service.workspace(ticket)
        memory_board_id = cls._memory_board_id(ctx, ticket)

        ref_files = sorted(changed_files)

        # Persist artifacts (no memory update — no agent ran).
        cls._persist_pass_artifacts(
            ws,
            ticket,
            ic,
            summary,
            ref_files,
            "",
            settings,
            memory_board_id,
        )

        # Run scope guardrail.
        guardrail = cls._run_scope_guardrail(
            ctx,
            ticket,
            repo_dir,
            branch,
            summary,
            ref_files,
            ic.file_map,
            settings,
            ic.spec,
            ic.feedback,
        )
        if guardrail.action == "return":
            return _SinglePassResult(next_action="return", outcome=guardrail.outcome)

        new_file_map = (
            guardrail.file_map if guardrail.file_map is not None else ic.file_map
        )
        new_feedback = (
            guardrail.feedback
            if guardrail.action in ("continue", "skip_iteration")
            else ic.feedback
        )
        new_ic = _ImplementContext(
            spec=ic.spec,
            memory_text=ic.memory_text,
            reference_files=[{"path": p} for p in ref_files],
            file_map=new_file_map,
            feedback=new_feedback,
            previous_attempt_summary=summary,
            open_thread_ids=ic.open_thread_ids,
        )
        if guardrail.action == "continue":
            return _SinglePassResult(next_action="retry", feedback=None, ic=new_ic)

        # Route to test evaluation.
        return cls._evaluate_test_results(  # type: ignore[no-any-return]
            ctx,
            ticket,
            repo_dir,
            branch,
            settings,
            ic,
            new_ic,
            summary,
            ref_files,
            None,  # new_msgs
            False,  # no_change_needed
            "",  # no_change_rationale
            False,  # resuming
            1,  # attempt
            max(1, settings.max_fix_iterations),  # max_iters
            extra_roots,
        )

    @staticmethod
    def _find_insertion_point(
        spec: str,
        code: str,
        file_lines: list[str],
    ) -> int | None:
        """Try to determine where in *file_lines* to insert *code* from *spec* context.

        Looks at the text preceding the code block in *spec* for hints:
        - "after the imports" / "after imports" → after last import line
        - "after line N" → after line N
        - "before line N" → before line N
        - "at the end" / "end of file" → at end
        - "before class"/"before def" → before first class/def

        Returns a 0-based line index or ``None`` if no hint was found.
        """
        # Find the code block in the spec to get its preceding context.
        escaped = _re.escape(code[:80])
        pattern = _re.compile(r"(.*?)```\w*\n" + escaped, _re.DOTALL)
        m = pattern.search(spec)
        if not m:
            return None

        before = m.group(1)
        # Take the last 10 lines of preceding context.
        context_lines = before.split("\n")[-10:]
        context = "\n".join(context_lines)

        # "after the imports" / "after imports"
        if _re.search(r"after\s+(the\s+)?imports?", context, _re.IGNORECASE):
            for i in range(len(file_lines) - 1, -1, -1):
                stripped = file_lines[i].lstrip()
                if stripped.startswith(("import ", "from ")):
                    return i + 1
            # No imports found — insert at top.
            return 0

        # "after line N"
        lm = _re.search(r"after\s+line\s+(\d+)", context, _re.IGNORECASE)
        if lm:
            n = int(lm.group(1))
            return min(n, len(file_lines))

        # "before line N"
        lm = _re.search(r"before\s+line\s+(\d+)", context, _re.IGNORECASE)
        if lm:
            n = int(lm.group(1))
            return max(0, n - 1)

        # "at the end" / "end of file" / "append"
        if _re.search(
            r"(at\s+the\s+end|end\s+of\s+file|append|bottom)",
            context,
            _re.IGNORECASE,
        ):
            return len(file_lines)

        # "before class X" / "before the class"
        if _re.search(r"before\s+(the\s+)?class\b", context, _re.IGNORECASE):
            for i, line in enumerate(file_lines):
                if line.lstrip().startswith("class "):
                    return i
            return None

        # "before def X" / "before the function"
        if _re.search(r"before\s+(the\s+)?(def|function)\b", context, _re.IGNORECASE):
            for i, line in enumerate(file_lines):
                if line.lstrip().startswith("def "):
                    return i
            return None

        return None
