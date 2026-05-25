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

from pathlib import Path

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
        self, definitions_dir: Path | None = None,
    ) -> dict[str, ExpertDefinition]:
        """Scan *definitions_dir* for ``*.yaml`` files and return a dict
        of ``{domain: ExpertDefinition}``.

        *definitions_dir* defaults to the repo-root ``expert_definitions/``
        directory.  Raises ``FileNotFoundError`` if the directory does not
        exist or contains no ``.yaml`` files (fail-fast at startup).
        """
        from .expert_loader import load_expert_definition

        if definitions_dir is None:
            definitions_dir = (
                Path(__file__).parent.parent.parent.parent
                / "expert_definitions"
            )

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

    def create_expert(self, definition: ExpertDefinition) -> "AgentHandle":
        """Build (or retrieve a cached) pydantic-ai agent for *definition*.

        On first call per domain the agent is constructed via
        ``build_agent`` and cached; subsequent calls return the same
        ``AgentHandle`` object.

        Tool resolution maps the string names in ``definition.tools`` to
        actual pydantic-ai callables:
        - ``fs_tools`` (``read_file``, ``write_file``, ``edit_file``,
          ``delete_file``, ``list_dir``, ``run_command``) are filtered from
          ``build_fs_tools``.
        - ``explore`` is added via ``make_explore_tool`` when present.

        The ``report_issue`` tool is always injected by ``build_agent``
        (default ``report_issue=True``).

        Model fallback: ``definition.model or self._settings.model``.
        """
        from .base import build_agent

        if definition.domain in self._cache:
            return self._cache[definition.domain]

        model_name = definition.model or self._settings.model

        # Build and filter fs tools.
        from .fs_tools import build_fs_tools

        all_fs = build_fs_tools(self._repo_dir, self._settings)
        tools = [t for t in all_fs if t.__name__ in definition.tools]

        # Optionally add explore tool.
        if "explore" in definition.tools:
            from .explore import make_explore_tool

            tools.append(make_explore_tool(self._settings, self._repo_dir))

        agent = build_agent(
            self._settings,
            system_prompt=definition.system_prompt,
            tools=tools,
            model_name=model_name,
            skills=definition.skills,
            name=f"expert:{definition.domain}",
        )

        self._cache[definition.domain] = agent
        return agent

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
