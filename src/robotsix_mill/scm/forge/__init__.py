"""Forge abstraction: Forge base class, GitHub adapter, GitLab adapter, and auth module."""

from .base import Forge, NotConfiguredError, _detect_forge_kind, get_forge

__all__ = [
    "Forge",
    "NotConfiguredError",
    "_detect_forge_kind",
    "get_forge",
]
