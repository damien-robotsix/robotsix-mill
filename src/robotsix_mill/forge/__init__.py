"""Forge abstraction: Forge base class, GitHub adapter, GitLab stub, and auth module."""

from .base import Forge, get_forge, NotConfiguredError, RepoInfo

__all__ = ["Forge", "get_forge", "RepoInfo", "NotConfiguredError"]
