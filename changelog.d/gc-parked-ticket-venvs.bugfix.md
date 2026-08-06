Data-dir GC now reclaims the ``.venv`` inside workspaces of PARKED
tickets — ``BLOCKED``, ``HUMAN_MR_APPROVAL`` and ``HUMAN_ISSUE_APPROVAL``.
Previously only terminal states (``CLOSED`` / ``EPIC_CLOSED`` /
``ANSWERED``) were reclaimable, so a blocked ticket pinned its whole
dependency tree indefinitely. Measured on the deploy box 2026-08-06: 157
parked workspaces holding 45 GB of ``.venv``, 34 GB of it under
``BLOCKED`` — on the very volume whose exhaustion had blocked 146 of
those tickets, a loop that could not drain on its own. Only ``.venv`` is
removed; the clone, its git history and any uncommitted work stay
inspectable for the human the ticket is parked for, and ``uv sync``
rebuilds the venv on resume from the shared package cache. Guarded by a
1-hour park-age threshold and a live-sandbox check so a ticket resumed
mid-pass never loses its venv under a running sync. Knobs:
``data_dir_gc_prune_parked_venvs`` (default on) and
``data_dir_gc_prune_parked_venvs_age_seconds``.
