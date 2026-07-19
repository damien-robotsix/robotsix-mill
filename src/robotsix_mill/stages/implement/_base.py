"""Typing-only base shim for the implement stage mixins.

Pure leaf (Pattern A): a ``TYPE_CHECKING``-only declaration of the
cross-mixin private methods so that ``cls.<method>`` / ``self.<method>``
calls — which resolve at runtime through the assembled
:class:`~.core.ImplementStage` MRO — type-check without any sibling
import. At runtime ``_ImplementStageBase`` is an empty class; the method
declarations exist only for the type checker.

This module imports **nothing** from its siblings (no mixin, no
``core``, not even ``_shared``), so it keeps the package import graph an
acyclic DAG.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar


class _ImplementStageBase:
    """Empty at runtime; declares the cross-mixin method seams for mypy."""

    if TYPE_CHECKING:
        # Declared as ``ClassVar[Any]`` so both ``cls.<m>`` and
        # ``self.<m>`` resolve, and the real ``@classmethod`` definitions
        # in the mixins override them compatibly (``Any``).
        _load_implement_context: ClassVar[Any]
        _run_scope_guardrail: ClassVar[Any]
        _resolve_language_instructions: ClassVar[Any]
        _select_agent_model: ClassVar[Any]
        _invoke_implement_agent: ClassVar[Any]
        _maybe_handle_pause: ClassVar[Any]
        _persist_pass_artifacts: ClassVar[Any]
        _evaluate_test_results: ClassVar[Any]
        _run_single_implement_pass: ClassVar[Any]
        _run_prerequisite_gate: ClassVar[Any]
        _run_baseline_check: ClassVar[Any]
        _baseline_fix_title: ClassVar[Any]
        _baseline_fix_already_resolved: ClassVar[Any]
        _spawn_baseline_fix: ClassVar[Any]
        _memory_board_id: ClassVar[Any]
        _implement_loop: ClassVar[Any]
        _any_repo_has_changes: ClassVar[Any]
        _edits_formatter_reverted: ClassVar[Any]
        _build_edit_claim_diagnostic: ClassVar[Any]
        _claimed_gitignored_edits: ClassVar[Any]
        _finalize: ClassVar[Any]
        _clone_and_branch: ClassVar[Any]
