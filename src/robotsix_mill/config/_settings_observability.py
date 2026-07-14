"""Settings field mixin: memory paths, tracing, notifications.

Field-only pydantic mixin extracted from the monolithic ``Settings``
model to keep ``settings.py`` under 800 lines. Assembled into the final
``Settings`` class in ``config/settings.py``.
"""

from __future__ import annotations

from pathlib import Path
from pydantic import BaseModel, Field


class _ObservabilitySettings(BaseModel):
    # --- action-agent memory paths ---
    # Path to the implement agent's Markdown memory ledger. Override to
    # pin a specific path; unset (default) derives <data_dir>/implement_memory.md.
    implement_memory_path: Path | None = Field(
        default=None,
        description="Path to the implement agent's Markdown memory ledger. When unset, derives from data_dir/board/implement_memory.md.",
    )
    # Path to the refine agent's Markdown memory ledger. Override to
    # pin a specific path; unset (default) derives <data_dir>/refine_memory.md.
    refine_memory_path: Path | None = Field(
        default=None,
        description="Path to the refine agent's Markdown memory ledger. When unset, derives from data_dir/board/refine_memory.md.",
    )
    # Path to the document agent's Markdown memory ledger. Override to
    # pin a specific path; unset (default) derives <data_dir>/doc_memory.md.
    doc_memory_path: Path | None = Field(
        default=None,
        description="Path to the document agent's Markdown memory ledger. When unset, derives from data_dir/board/doc_memory.md.",
    )
    # Path to the ci-fix agent's Markdown memory ledger. Override to
    # pin a specific path; unset (default) derives <data_dir>/ci_fix_memory.md.
    ci_fix_memory_path: Path | None = Field(
        default=None,
        description="Path to the CI-fix agent's Markdown memory ledger. When unset, derives from data_dir/board/ci_fix_memory.md.",
    )
    # Path to the review-revision agent's Markdown memory ledger.
    # Override to pin a specific path; unset (default) derives
    # <data_dir>/review_revision_memory.md.
    review_revision_memory_path: Path | None = Field(
        default=None,
        description="Path to the review-revision agent's Markdown memory ledger. When unset, derives from data_dir/board/review_revision_memory.md.",
    )
    # Path to the rebase agent's Markdown memory ledger. Override to
    # pin a specific path; unset (default) derives <data_dir>/rebase_memory.md.
    rebase_memory_path: Path | None = Field(
        default=None,
        description="Path to the rebase agent's Markdown memory ledger. When unset, derives from data_dir/board/rebase_memory.md.",
    )
    # Path to the ci-fix agent's structured pattern memory.  Override
    # to pin a specific path; unset (default) derives
    # <data_dir>/ci_patterns.json.
    ci_patterns_path: Path | None = Field(
        default=None,
        description="Path to the CI-fix agent's structured pattern memory (JSON). When unset, derives from data_dir/ci_patterns.json.",
    )
