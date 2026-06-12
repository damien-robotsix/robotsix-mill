"""Shared constant definitions used across the robotsix_mill codebase.

These are definitions that would otherwise be duplicated in multiple
modules (e.g. binary-file extension sets).  They live in ``core/``
because ``core/`` already hosts cross-cutting concerns like models,
states, and text utilities.
"""

from __future__ import annotations

# File extensions that are likely binary — should be skipped during
# text preview / log traversal.
BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".gz",
        ".zip",
        ".tar",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".pkl",
        ".pickle",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".svg",
        ".ico",
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".mp3",
        ".mp4",
        ".wav",
        ".avi",
        ".mov",
        ".pyc",
        ".pyo",
        ".so",
        ".dll",
        ".exe",
        ".bin",
        ".dat",
        ".elf",
    }
)
