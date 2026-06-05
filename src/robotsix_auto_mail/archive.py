"""Self-managed archive folder structure for robotsix-auto-mail.

robotsix-auto-mail manages its own archive folder hierarchy, independent of
any pre-existing mailbox layout.  On the first run a quick LLM call proposes
an appropriate layout (based on the mailbox's existing folders) rooted at
``robotsix-mail-archive``; the chosen structure is then remembered in the
``watermark`` table so subsequent runs reuse it without re-asking the LLM or
recreating folders.

The ``pydantic_ai`` and ``openrouter_deepseek`` provider imports are lazy
to keep module-load time low and to avoid requiring the optional provider
extra for the deterministic import path, mirroring
:mod:`robotsix_auto_mail.detect`.
"""

from __future__ import annotations

import json
import os
import sqlite3

import pydantic
from robotsix_llmio.core import Tier

from robotsix_auto_mail.db import get_watermark, set_watermark
from robotsix_auto_mail.imap import ImapClient

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Root folder under which all managed archive folders live.
ARCHIVE_ROOT = "robotsix-mail-archive"

#: Watermark key owned by this module (the same way ``fetch.py`` owns
#: ``"imap_uid"``).
_ARCHIVE_WATERMARK_KEY = "archive_structure"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ArchiveError(Exception):
    """Raised when determining the archive structure via the LLM fails."""


# ---------------------------------------------------------------------------
# Pydantic model — structured LLM output contract
# ---------------------------------------------------------------------------


class ArchiveStructure(pydantic.BaseModel):
    """Structured output the LLM must return — validated by pydantic.

    Each entry in ``folders`` is a sub-path relative to the archive root,
    using ``/`` as the separator (the list may be empty → just the root).
    """

    folders: list[str] = pydantic.Field(default_factory=list)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def _build_archive_system_prompt(archive_root: str) -> str:
    """Build the LLM system prompt, rooted at *archive_root*."""
    return (
        "You are an email archive organisation expert. Given the list of "
        "folders that already exist in a user's mailbox, propose an "
        f"appropriate archive folder layout rooted at `{archive_root}`.\n"
        "\n"
        "Return a JSON object with a `folders` field: a list of sub-paths "
        f"relative to the root `{archive_root}`, using `/` as the hierarchy "
        "separator. Do NOT include the root itself in the list, and do NOT "
        "prefix entries with the root. The list may be empty if just the "
        "root is appropriate.\n"
        "\n"
        "Return ONLY the JSON object matching the schema — no explanation, no "
        "markdown fences."
    )


# ---------------------------------------------------------------------------
# Core LLM call
# ---------------------------------------------------------------------------


def determine_archive_structure(
    existing_folders: list[str],
    *,
    archive_root: str = ARCHIVE_ROOT,
    api_key: str | None = None,
    tier: Tier = Tier.CHEAP,
) -> list[str]:
    """Ask an LLM to propose an archive folder layout under the root.

    Args:
        existing_folders: Names of the folders already present in the
            mailbox, used to inform the proposed layout.
        api_key: OpenRouter API key.  Defaults to the ``LLM_API_KEY`` env
            var.  Required unless the env var is set.
        tier: LLM tier to use.  ``Tier.CHEAP`` (default).

    Returns:
        A list of sub-paths relative to the archive root (``/``-separated).

    Raises:
        ArchiveError: If the API key is missing, the LLM returns an invalid
            response, or any other error occurs.
    """
    # -- resolve API key --
    resolved_key = api_key or os.environ.get("LLM_API_KEY", "")
    if not resolved_key:
        raise ArchiveError(
            "No LLM API key found — set the LLM_API_KEY environment "
            "variable or add an `llm.api_key` entry to your config file"
        )

    # -- lazy import so the rest of the CLI works without the
    #    openrouter_deepseek extra --
    from pydantic_ai import PromptedOutput
    from robotsix_llmio.openrouter_deepseek import OpenRouterDeepseekProvider

    # -- build agent --
    llm_provider = OpenRouterDeepseekProvider(api_key=resolved_key)
    agent_handle = llm_provider.build_agent(
        tier=tier,
        system_prompt=_build_archive_system_prompt(archive_root),
        output_type=PromptedOutput(ArchiveStructure),
    )

    # -- build the user message --
    user_message = "Existing mailbox folders:\n" + "\n".join(existing_folders)

    # -- call LLM --
    try:
        result = llm_provider.call_with_retry(
            lambda: agent_handle.run_sync(user_message),
            what="archive structure",
        )
    except Exception as exc:
        raise ArchiveError(str(exc)) from exc
    finally:
        agent_handle.close()

    structure: ArchiveStructure = result.output
    return structure.folders


# ---------------------------------------------------------------------------
# Setup / persistence
# ---------------------------------------------------------------------------


def setup_archive(
    conn: sqlite3.Connection,
    client: ImapClient,
    *,
    archive_root: str = ARCHIVE_ROOT,
    api_key: str | None = None,
    tier: Tier = Tier.CHEAP,
) -> list[str]:
    """Ensure the managed archive folder structure exists and is remembered.

    On the first run (no persisted structure) this lists the mailbox's
    folders, asks the LLM for an appropriate layout under
    :data:`ARCHIVE_ROOT`, creates the missing folders, and persists the
    resulting full-name list in the ``watermark`` table.  On subsequent runs
    the persisted list is returned directly without listing folders, calling
    the LLM, or creating anything.

    When no LLM API key is resolvable the LLM is never called — the archive
    falls back to just the root folder so ingestion is never blocked.

    Args:
        conn: Open SQLite connection.
        client: Connected IMAP client.
        api_key: OpenRouter API key.  Defaults to the ``LLM_API_KEY`` env var.
        tier: LLM tier to use.  ``Tier.CHEAP`` (default).

    Returns:
        The list of full archive folder names that exist after setup.
    """
    # -- already-remembered short-circuit --
    remembered = get_watermark(conn, _ARCHIVE_WATERMARK_KEY)
    if remembered is not None:
        parsed: list[str] = json.loads(remembered)
        return parsed

    # -- first run: inspect the mailbox --
    existing = client.list_folders()
    delimiter = next(
        (f.delimiter for f in existing if f.delimiter), "/"
    )

    # -- determine relative sub-paths (LLM, or fall back to root only) --
    resolved_key = api_key or os.environ.get("LLM_API_KEY", "")
    if resolved_key:
        subpaths = determine_archive_structure(
            [f.name for f in existing],
            archive_root=archive_root,
            api_key=resolved_key,
            tier=tier,
        )
    else:
        subpaths = []

    # -- build the full set of folder names to ensure --
    structure: list[str] = [archive_root]
    for subpath in subpaths:
        translated = subpath.replace("/", delimiter)
        structure.append(archive_root + delimiter + translated)

    # -- create only the missing targets --
    existing_names = {f.name for f in existing}
    for name in structure:
        if name not in existing_names:
            client.create_folder(name)

    # -- persist and return --
    set_watermark(conn, _ARCHIVE_WATERMARK_KEY, json.dumps(structure))
    return structure
