"""Typing-only base shim for the merge stage mixins.

Pure leaf (Pattern A): a ``TYPE_CHECKING``-only declaration of the
cross-mixin private methods so that ``cls.<method>`` / ``self.<method>``
calls — which resolve at runtime through the assembled
:class:`~.core.MergeStage` MRO — type-check without any sibling
import. At runtime ``_MergeStageBase`` is an empty class; the method
declarations exist only for the type checker.

This module imports **nothing** from its siblings (no mixin, no
``core``, not even ``_shared``), so it keeps the package import graph an
acyclic DAG.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar


class _MergeStageBase:
    """Empty at runtime; declares the cross-mixin method seams for mypy."""

    if TYPE_CHECKING:
        # Declared as ``ClassVar[Any]`` so both ``cls.<m>`` and
        # ``self.<m>`` resolve, and the real method definitions
        # in the mixins / core override them compatibly (``Any``).
        _maybe_comment: ClassVar[Any]
        _cleanup_branch_on_done: ClassVar[Any]
        _review_changes_requested_outcome: ClassVar[Any]
        _auto_merge_eligible: ClassVar[Any]
