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

    # --- tracing (optional) ---
    langfuse_base_url: str | None = Field(
        default=None,
        alias="LANGFUSE_BASE_URL",
        description="Langfuse instance base URL for LLM observability. Unset disables tracing.",
    )
    langfuse_public_key: str | None = Field(
        default=None,
        alias="LANGFUSE_PUBLIC_KEY",
        description="Langfuse public key for LLM observability tracing.",
    )
    langfuse_secret_key: str | None = Field(
        default=None,
        alias="LANGFUSE_SECRET_KEY",
        description="Langfuse secret key for LLM observability tracing.",
    )
    langfuse_project_id: str | None = Field(
        default=None,
        alias="LANGFUSE_PROJECT_ID",
        description="Langfuse project ID for trace attribution.",
    )

    # --- notifications (optional) ---
    ntfy_url: str | None = Field(
        default=None,
        alias="NTFY_URL",
        description="ntfy server URL for push notifications.",
    )
    ntfy_token: str | None = Field(
        default=None,
        alias="NTFY_TOKEN",
        description="ntfy access token for authenticated push notifications.",
    )
