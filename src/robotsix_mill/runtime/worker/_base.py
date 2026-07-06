"""Typing-only shared base for the Worker mixins.

The concrete ``Worker`` class (``core.py``) is assembled from
``PeriodicPassesMixin`` and ``PollLoopsMixin``. Those mixin methods access
attributes set in ``Worker.__init__`` and call methods defined on the
assembled class or on the sibling mixin. This base declares those names —
under ``TYPE_CHECKING`` only, so it has **no** runtime effect — so the type
checker can resolve cross-class ``self`` access. The real attributes and
methods on the assembled ``Worker`` override these declarations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, ParamSpec, TypeVar

if TYPE_CHECKING:
    import asyncio
    from pathlib import Path

    from ...config import RepoConfig
    from ...stages import StageContext
    from ..run_registry import RunRegistry

    _P = ParamSpec("_P")
    _R = TypeVar("_R")


class _WorkerBase:
    """Typing-only shim; see module docstring."""

    if TYPE_CHECKING:
        # --- data attributes (set in Worker.__init__) ---
        ctx: StageContext
        run_registry: RunRegistry | None
        run_registries: dict[str, RunRegistry]
        _inflight_passes: set[asyncio.Task]

        # --- class constants (defined on Worker / a sibling mixin) ---
        _META_BOARD: str
        _PERIODIC_POLL_TICK_SECONDS: int

        # --- methods defined on Worker core / a sibling mixin ---
        def _registry_for(self, repo_config: object) -> RunRegistry | None: ...

        def _initial_delay(
            self,
            kind: str,
            interval: int,
            repo_id: str = ...,
            registry: object = ...,
        ) -> float: ...

        def _find_config_clone_dir(self, repo_config: object) -> Path | None: ...

        def _resolve_periodic_schedule(
            self,
            label: str,
            repo_config: object,
            settings_interval_attr: str,
            settings_enabled_attr: str | None = ...,
        ) -> tuple[bool, int]: ...

        async def _fire_periodic_pass(
            self,
            label: str,
            runner_fn: object,
            repo_config: object,
        ) -> None: ...

        async def _tracked_to_thread(
            self, fn: Callable[_P, _R], *args: _P.args, **kwargs: _P.kwargs
        ) -> _R: ...

        def _build_periodic_workflow_runner(self, wf: object) -> object: ...

        async def _run_periodic_workflow_loop(
            self, repo_config: object, wf: object, clone_dir: object
        ) -> None: ...

        def _has_periodic_presence(self, repo_config: object, label: str) -> bool: ...

        async def _run_bespoke_loop(
            self,
            repo_config: RepoConfig,
            definition: object,
            clone_dir: object,
        ) -> None: ...

        async def _trace_health_poll_loop(self) -> None: ...
