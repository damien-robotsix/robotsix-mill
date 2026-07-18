"""The survey agent: discovers and learns from similar open-source
projects, proposing concrete improvements for the current repo.

Seam: tests monkeypatch ``run_survey_agent``. Structured output so
the runner has a clear result to work with.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from ..config import Settings
from .periodic_base import (
    PeriodicAgentResult,
    make_agent_runner,
)

logger = logging.getLogger(__name__)

# One subject per run → one proposal max, plus an optional companion
# standards-board ticket when the finding is fleet-wide.  The previous
# limit of 5 encouraged the agent to sweep the codebase for five gaps
# in a single ~$1 run that routinely blew the 12-request budget.  The
# rewritten prompt (agent_definitions/periodic/survey.yaml) caps the
# agent at one focused subject per run; this code-side cap is defence
# in depth.  A fleet-wide finding may produce two drafts: one on the
# standards board (codification proposal) and one on the current
# repo's board (repo-specific mechanical work).
MAX_GAPS = 2

SurveyResult = PeriodicAgentResult

# Hard-coded standards repo — mirrors the URL in
# ``src/robotsix_mill/agents/standards.py`` and the prompt references
# in agent_definitions/periodic/{audit,survey}.yaml.  Cloned once per
# data_dir into a cache subdirectory so the survey agent can browse
# the standards tree directly via its filesystem tools (``list_dir``,
# ``read_file``, ``explore``).
_STANDARDS_REPO_URL = "https://github.com/damien-robotsix/robotsix-standards"
_STANDARDS_CACHE_SUBDIR = ("standards_cache", "repo")


def _ensure_standards_repo(settings: Settings) -> Path | None:
    """Return a local checkout of robotsix-standards, or ``None``.

    Clones a shallow copy on first call; subsequent calls pull
    (best-effort).  Failures degrade gracefully — the agent works
    without standards when the clone is unavailable.
    """
    cache_dir = settings.data_dir.joinpath(*_STANDARDS_CACHE_SUBDIR)

    if (cache_dir / ".git").exists():
        # Already cloned — try a fast-forward pull (stale is fine).
        try:
            subprocess.run(  # noqa: S603 — all args are repo-controlled constants
                ["git", "-C", str(cache_dir), "pull", "--quiet", "--ff-only"],  # noqa: S607 — git is on PATH
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except Exception:
            logger.debug("standards repo pull failed", exc_info=True)
        return cache_dir

    try:
        cache_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(  # noqa: S603 — all args are repo-controlled constants
            [  # noqa: S607 — git is on PATH
                "git",
                "clone",
                "--quiet",
                "--depth=1",
                _STANDARDS_REPO_URL,
                str(cache_dir),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        logger.info("standards repo cloned to %s", cache_dir)
        return cache_dir
    except Exception:
        logger.warning("standards repo clone failed", exc_info=True)
        return None


def _survey_dynamic_kwargs(settings: Settings) -> dict[str, Any]:
    from pydantic_ai.usage import UsageLimits

    kwargs: dict[str, Any] = {
        "usage_limits": UsageLimits(request_limit=settings.survey_request_limit),
    }

    standards_root = _ensure_standards_repo(settings)
    if standards_root is not None:
        kwargs["extra_roots"] = [standards_root]

    return kwargs


run_survey_agent = make_agent_runner(
    definition_name="survey",
    prompt_tail="Survey similar open-source projects and return your proposals.",
    max_gaps=MAX_GAPS,
    include_forge_url=True,
    dynamic_kwargs_fn=_survey_dynamic_kwargs,
    fallback_level=3,
)
