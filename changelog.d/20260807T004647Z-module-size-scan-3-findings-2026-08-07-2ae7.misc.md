Split three large modules to improve cohesion:
  - ``src/robotsix_mill/vcs/git_diff.py`` extracted from ``git_ops.py`` (14 diff-analysis functions)
  - ``tests/agents/test_refine_dedup.py`` extracted from ``test_refine.py`` (25 dedup-guard tests)
  - ``src/robotsix_mill/sandbox/_fetch.py`` and ``_lifecycle.py`` extracted from ``sandbox/__init__.py``
