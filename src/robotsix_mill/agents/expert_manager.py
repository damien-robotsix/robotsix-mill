"""Expert lifecycle manager.

``ExpertManager`` is the single runtime owner for expert agent
instances.  It loads declarative ``ExpertDefinition`` objects from
YAML files in ``expert_definitions/`` and instantiates, caches,
retrieves, and cleans up the corresponding pydantic-ai agents.

Usage::

    from robotsix_mill.agents.expert_manager import ExpertManager

    mgr = ExpertManager(settings, repo_dir)
    defs = mgr.load_definitions()          # {domain: ExpertDefinition}
    py_agent = mgr.create_expert(defs["python-backend"])
    # ... later ...
    mgr.close_all()
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from robotsix_mill._resources import expert_definitions_dir
from ..config import Settings
from .expert_loader import ExpertDefinition


class ExpertManager:
    """Single lifecycle owner for expert agent instances.

    Loads ``ExpertDefinition`` objects from YAML files and lazily builds
    cached pydantic-ai agents on demand.  Each domain maps to exactly
    one ``AgentHandle`` — repeated calls to ``create_expert`` for the
    same domain return the identical cached instance.
    """

    def __init__(self, settings: Settings, repo_dir: Path) -> None:
        self._settings = settings
        self._repo_dir = Path(repo_dir)
        self._cache: dict[str, "AgentHandle"] = {}

    # -- definition loading ------------------------------------------------

    def load_definitions(
        self,
        definitions_dir: Path | None = None,
    ) -> dict[str, ExpertDefinition]:
        """Scan *definitions_dir* for ``*.yaml`` files and return a dict
        of ``{domain: ExpertDefinition}``.

        *definitions_dir* defaults to the repo-root ``expert_definitions/``
        directory.  Raises ``FileNotFoundError`` if the directory does not
        exist or contains no ``.yaml`` files (fail-fast at startup).
        """
        from .expert_loader import load_expert_definition

        if definitions_dir is None:
            definitions_dir = expert_definitions_dir()

        if not definitions_dir.is_dir():
            raise FileNotFoundError(
                f"Expert definitions directory not found: {definitions_dir}"
            )

        yaml_files = sorted(definitions_dir.glob("*.yaml"))
        if not yaml_files:
            raise FileNotFoundError(
                f"No YAML definition files found in: {definitions_dir}"
            )

        result: dict[str, ExpertDefinition] = {}
        for yaml_file in yaml_files:
            definition = load_expert_definition(yaml_file)
            result[definition.domain] = definition

        return result

    # -- agent lifecycle ---------------------------------------------------

    def create_expert(
        self,
        definition: ExpertDefinition,
        *,
        output_type: Any = None,
        memory_text: str = "",
    ) -> "AgentHandle":
        """Build (or retrieve a cached) pydantic-ai agent for *definition*.

        On first call per domain the agent is constructed via
        ``build_agent`` and cached; subsequent calls return the same
        ``AgentHandle`` object. The cache key is the domain alone — so
        if you pass a different ``output_type`` or ``memory_text`` to
        the second call, you'll still get the first call's cached agent.
        Callers that need a non-cached behaviour (e.g. a fresh memory
        block) should call :meth:`remove_expert` first.

        Tool resolution maps the string names in ``definition.tools`` to
        actual pydantic-ai callables:
        - ``fs_tools`` (``read_file``, ``write_file``, ``edit_file``,
          ``delete_file``, ``list_dir``, ``run_command``) are filtered from
          ``build_fs_tools``.
        - ``explore`` is added via ``make_explore_tool`` when present.

        The ``report_issue`` tool is always injected by ``build_agent``
        (default ``report_issue=True``).

        Model: resolved from ``definition.level`` (default level 2).

        ``output_type`` — when non-None, forwarded to ``build_agent`` so
        the expert returns structured output (e.g. ``PromptedOutput
        (ImplementResult)``). When ``None`` (default), preserves the
        existing ``str``-output behaviour for back-compat with callers
        that don't expect structured output.

        ``memory_text`` — when non-empty, appended to the system prompt
        inside a ``<memory>…</memory>`` block so the expert can read its
        own ledger. When empty (default), no memory block is injected.
        """
        from .base import build_agent

        if definition.domain in self._cache:
            return self._cache[definition.domain]

        # Build and filter fs tools.
        from .fs_tools import build_fs_tools

        all_fs = build_fs_tools(self._repo_dir, self._settings)
        tools = [t for t in all_fs if t.__name__ in definition.tools]

        # Optionally add explore tool.
        if "explore" in definition.tools:
            from .explore import make_explore_tool

            tools.append(make_explore_tool(self._settings, self._repo_dir))

        system_prompt = definition.system_prompt
        if memory_text:
            system_prompt = f"{system_prompt}\n\n<memory>\n{memory_text}\n</memory>"

        build_kwargs: dict[str, Any] = dict(
            system_prompt=system_prompt,
            tools=tools,
            level=definition.level,
            skills=definition.skills,
            name=f"expert:{definition.domain}",
        )
        if output_type is not None:
            build_kwargs["output_type"] = output_type

        agent = build_agent(self._settings, **build_kwargs)

        self._cache[definition.domain] = agent
        return agent

    # -- glob matching for module_paths ----------------------------------

    @staticmethod
    def _glob_to_regex(pattern: str) -> re.Pattern:
        """Convert a POSIX-style glob to an anchored regex.

        Semantics (intentionally narrow — designed for ``module_paths``,
        not full POSIX globbing):

        - ``**`` matches any number of path segments (including zero).
          ``src/**/*.py`` matches ``src/a.py``, ``src/a/b.py``, etc.
        - ``*`` matches any run of characters within a single segment
          (no ``/``).
        - ``?`` matches exactly one non-``/`` character.
        - All other regex metacharacters are escaped literally.

        The returned pattern is anchored (``^…$``) and intended for
        :py:meth:`re.Pattern.fullmatch`.
        """
        # Walk the pattern char-by-char so we can detect ``**`` vs ``*``
        # without ambiguity. ``re.escape`` would over-escape ``*``/``?``
        # which we then have to undo, so doing it longhand is clearer.
        i = 0
        out: list[str] = []
        while i < len(pattern):
            c = pattern[i]
            if c == "*" and i + 1 < len(pattern) and pattern[i + 1] == "*":
                # ``**`` — zero-or-more segments, including the trailing /.
                # Match either nothing (so `src/**/foo` covers `src/foo`)
                # or one-or-more segments followed by a slash.
                out.append(r"(?:.*/)?")
                i += 2
                # Swallow a following slash so the regex matches "src/foo"
                # for the pattern "src/**/foo" (zero intermediate segments).
                if i < len(pattern) and pattern[i] == "/":
                    i += 1
            elif c == "*":
                out.append(r"[^/]*")
                i += 1
            elif c == "?":
                out.append(r"[^/]")
                i += 1
            else:
                out.append(re.escape(c))
                i += 1
        return re.compile(f"^{''.join(out)}$")

    @staticmethod
    def match_module_paths(
        module_paths: list[str],
        file_path: str,
    ) -> bool:
        """Return ``True`` when *file_path* matches ANY pattern in
        *module_paths*.

        *file_path* is expected to be a POSIX-style relative path
        (e.g. ``"src/robotsix_mill/agents/coordinating.py"``).
        Empty *module_paths* returns ``False``.
        """
        for pattern in module_paths:
            if ExpertManager._glob_to_regex(pattern).fullmatch(file_path):
                return True
        return False

    def get_expert(self, domain: str) -> "AgentHandle | None":
        """Return the cached agent for *domain*, or ``None``.

        Never triggers creation — use ``create_expert`` for that.
        """
        return self._cache.get(domain)

    def remove_expert(self, domain: str) -> None:
        """Close and discard the cached agent for *domain*.

        Idempotent — a no-op when *domain* is not cached.
        """
        from .base import _safe_close

        agent = self._cache.pop(domain, None)
        if agent is not None:
            _safe_close(agent)

    def close_all(self) -> None:
        """Close every cached agent and clear the cache."""
        from .base import _safe_close

        for agent in self._cache.values():
            _safe_close(agent)
        self._cache.clear()
