"""Forge abstraction: Forge base class, GitHub adapter, GitLab adapter, and auth module."""

from .base import Forge, NotConfiguredError, RepoInfo, _detect_forge_kind, get_forge

__all__ = [
    "Forge",
    "NotConfiguredError",
    "RepoInfo",
    "_detect_forge_kind",
    "get_forge",
]
