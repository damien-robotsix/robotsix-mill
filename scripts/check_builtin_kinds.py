#!/usr/bin/env python3
"""Deterministic cross-validation of ``_BUILTIN_KINDS`` against its sync points.

Usage (from the repo root):
    python scripts/check_builtin_kinds.py

Cross-references the live source-of-truth objects — never re-parses source
— to catch ``_BUILTIN_KINDS`` drift across the five sync points:

    1. ``.robotsix-mill/periodic/`` presence files
    2. ``agent_definitions/periodic/`` YAML definitions
    3. ``src/robotsix_mill/runtime/routes/_passes.py`` pass-routing table
    4. ``agent_definitions/periodic/`` → every ``llm_agent`` entry in
       ``_BUILTIN_KINDS`` must have a matching YAML definition.
    5. ``src/robotsix_mill/runtime/worker/poll_loops.py`` → every
       ``schedule_only`` entry in ``_BUILTIN_KINDS`` must have a matching
       entry in ``PollLoopsMixin._SCHEDULE_ONLY_RUNNERS``.
    6. ``src/robotsix_mill/cli/__init__.py`` → every ``_PASS_REGISTRY``
       entry must have a matching CLI ``_RUNNERS`` entry with a matching
       function name (modulo ``_CLI_RUNNER_FUNC_MISMATCH_OK``).

Invariants (each contributes drift lines; the run fails if any fire):

    1. Every name in ``.robotsix-mill/periodic/`` must appear in
       ``_BUILTIN_KINDS``.
    2. Every name in ``agent_definitions/periodic/`` (excluding
       ``global_only`` kinds) must appear in ``_BUILTIN_KINDS``.
    3. Every ``schedule_only`` / ``llm_agent`` name in ``_passes.py``
       must have a matching entry in ``_BUILTIN_KINDS`` with the
       correct kind, modulo the intentional mismatches documented in
       ``_PASS_KIND_MISMATCH_OK``.
    4. Every ``llm_agent`` name in ``_BUILTIN_KINDS`` must have a
       corresponding YAML definition in ``agent_definitions/periodic/``.
    5. Every ``schedule_only`` name in ``_BUILTIN_KINDS`` must have a
       corresponding entry in ``PollLoopsMixin._SCHEDULE_ONLY_RUNNERS``.
    6. Every ``_PASS_REGISTRY`` entry must have a matching CLI
       ``_RUNNERS`` entry with a matching function name (modulo
       ``_CLI_RUNNER_FUNC_MISMATCH_OK``).

Exit codes:
    0 — every invariant holds; ``_BUILTIN_KINDS`` is in sync.
    1 — at least one invariant fired; details are printed to stderr.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Ensure the repo root and src/ are importable so 'import robotsix_mill'
# works when run as a flat script.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# ---------------------------------------------------------------------------
#  Explicit, commented exception sets
# ---------------------------------------------------------------------------

# Names in _PASS_REGISTRY whose kind intentionally differs from
# _BUILTIN_KINDS.  Each entry maps pass name → (pass_kind, builtin_kind).
_PASS_KIND_MISMATCH_OK: dict[str, tuple[str, str]] = {
    # _passes.py says "schedule_only" but they are cross-repo infra so
    # _BUILTIN_KINDS classifies them as "global_only".
    "langfuse_cleanup": ("schedule_only", "global_only"),
    "trace_health": ("schedule_only", "global_only"),
    # _passes.py says "llm_agent" but they are restricted to the
    # robotsix-mill repo itself so _BUILTIN_KINDS marks them "mill_only".
    "state_sync": ("llm_agent", "mill_only"),
    "frontend_sync": ("llm_agent", "mill_only"),
}

# Names in agent_definitions/periodic/ that are intentionally excluded
# from invariant-2 (global_only kinds that are NOT per-repo presence
# managed — they appear for mill-internal YAML wiring only).
_AGENT_DEF_GLOBAL_ONLY: frozenset[str] = frozenset(
    {
        "meta",
        "run_health",
    }
)

# Names where the CLI _RUNNERS function name intentionally differs from
# _PASS_REGISTRY's runner_func.  The CLI calls a non-wrapper function
# directly (without server-provided kwargs like ``settings`` or
# ``request``), while the pass registry routes to wrappers that supply
# those extras at runtime.
#
#   pass_name → (runner_func in _PASS_REGISTRY, function in _RUNNERS)
_CLI_RUNNER_FUNC_MISMATCH_OK: dict[str, tuple[str, str]] = {
    "trace_health": (
        "run_trace_health_pass",
        "run_trace_health_check",
    ),
    "langfuse_cleanup": (
        "run_langfuse_cleanup_pass_wrapper",
        "run_langfuse_cleanup_pass",
    ),
    "meta": (
        "run_meta_pass_wrapper",
        "run_meta_pass",
    ),
    "run_health": (
        "run_run_health_pass_wrapper",
        "run_run_health_pass",
    ),
}


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------


def _yaml_stems(directory: Path) -> set[str]:
    """Return the set of ``.yaml`` stem names in *directory*."""
    stems: set[str] = set()
    if not directory.is_dir():
        return stems
    for child in directory.iterdir():
        if child.is_file() and child.suffix == ".yaml":
            stems.add(child.stem)
    return stems


# ---------------------------------------------------------------------------
#  Invariant checks
# ---------------------------------------------------------------------------


def check_presence_files_in_builtin(
    presence_stems: set[str],
    builtin_kinds: dict[str, str],
) -> list[str]:
    """Invariant 1: every ``.robotsix-mill/periodic/*.yaml`` → ``_BUILTIN_KINDS``."""
    drift: list[str] = []
    for name in sorted(presence_stems):
        if name not in builtin_kinds:
            drift.append(
                f".robotsix-mill/periodic/{name}.yaml exists but "
                f"{name!r} is not in _BUILTIN_KINDS"
            )
    return drift


def check_agent_defs_in_builtin(
    agent_def_stems: set[str],
    builtin_kinds: dict[str, str],
    global_only: frozenset[str],
) -> list[str]:
    """Invariant 2: every ``agent_definitions/periodic/*.yaml``
    (excluding global_only) → ``_BUILTIN_KINDS``."""
    drift: list[str] = []
    for name in sorted(agent_def_stems):
        if name in global_only:
            continue
        if name not in builtin_kinds:
            drift.append(
                f"agent_definitions/periodic/{name}.yaml exists but "
                f"{name!r} is not in _BUILTIN_KINDS"
            )
    return drift


def check_pass_registry_vs_builtin(
    pass_registry: dict[str, dict[str, Any]],
    builtin_kinds: dict[str, str],
    mismatches_ok: dict[str, tuple[str, str]],
) -> list[str]:
    """Invariant 3: every schedule_only / llm_agent pass → _BUILTIN_KINDS
    with correct kind (modulo intentional mismatches)."""
    drift: list[str] = []
    for name, entry in sorted(pass_registry.items()):
        pass_kind = entry.get("kind")
        if pass_kind not in ("schedule_only", "llm_agent"):
            continue

        if name not in builtin_kinds:
            drift.append(
                f"_PASS_REGISTRY[{name!r}] has kind {pass_kind!r} but "
                f"{name!r} is missing from _BUILTIN_KINDS"
            )
            continue

        builtin_kind = builtin_kinds[name]

        if name in mismatches_ok:
            expected_pass_kind, expected_builtin_kind = mismatches_ok[name]
            if pass_kind != expected_pass_kind:
                drift.append(
                    f"_PASS_REGISTRY[{name!r}] kind is {pass_kind!r}, "
                    f"expected {expected_pass_kind!r} per "
                    f"_PASS_KIND_MISMATCH_OK"
                )
            if builtin_kind != expected_builtin_kind:
                drift.append(
                    f"_BUILTIN_KINDS[{name!r}] is {builtin_kind!r}, "
                    f"expected {expected_builtin_kind!r} per "
                    f"_PASS_KIND_MISMATCH_OK"
                )
        elif pass_kind != builtin_kind:
            drift.append(
                f"_PASS_REGISTRY[{name!r}] kind is {pass_kind!r} but "
                f"_BUILTIN_KINDS[{name!r}] is {builtin_kind!r} "
                f"(must match; if intentional, add to "
                f"_PASS_KIND_MISMATCH_OK)"
            )
    return drift


def check_builtin_llm_agents_have_def(
    builtin_kinds: dict[str, str],
    agent_def_stems: set[str],
) -> list[str]:
    """Invariant 4: every ``llm_agent`` in ``_BUILTIN_KINDS`` must have a
    corresponding YAML in ``agent_definitions/periodic/``."""
    drift: list[str] = []
    for name, kind in sorted(builtin_kinds.items()):
        if kind == "llm_agent" and name not in agent_def_stems:
            drift.append(
                f"_BUILTIN_KINDS[{name!r}] is llm_agent but "
                f"agent_definitions/periodic/{name}.yaml does not exist"
            )
    return drift


def check_schedule_only_runner_wiring(
    builtin_kinds: dict[str, str],
    schedule_only_runners: dict[str, str],
) -> list[str]:
    """Invariant 5: every ``schedule_only`` in ``_BUILTIN_KINDS`` must have a
    corresponding entry in ``PollLoopsMixin._SCHEDULE_ONLY_RUNNERS``."""
    drift: list[str] = []
    for name, kind in sorted(builtin_kinds.items()):
        if kind == "schedule_only" and name not in schedule_only_runners:
            drift.append(
                f"_BUILTIN_KINDS[{name!r}] is schedule_only but "
                f"{name!r} is missing from "
                f"PollLoopsMixin._SCHEDULE_ONLY_RUNNERS"
            )
    return drift


def check_pass_registry_cli_runners(
    pass_registry: dict[str, dict[str, Any]],
    cli_runners: dict[str, dict[str, str]],
    mismatches_ok: dict[str, tuple[str, str]],
) -> list[str]:
    """Invariant 6: every ``_PASS_REGISTRY`` entry must have a matching
    CLI ``_RUNNERS`` entry with a matching function name (modulo
    intentional mismatches in ``_CLI_RUNNER_FUNC_MISMATCH_OK``).

    The mapping from pass name (underscore form, e.g. ``data_dir_gc``) to
    CLI runner key (hyphen form, e.g. ``data-dir-gc``) is
    ``pass_name.replace('_', '-')``.
    """
    drift: list[str] = []
    for pass_name, entry in sorted(pass_registry.items()):
        cli_key = pass_name.replace("_", "-")
        if cli_key not in cli_runners:
            drift.append(
                f"_PASS_REGISTRY[{pass_name!r}] has no CLI _RUNNERS "
                f"entry for {cli_key!r}"
            )
            continue

        runner_func = cli_runners[cli_key]["function"]
        expected_func = entry.get("runner_func", "")

        if pass_name in mismatches_ok:
            exp_pass_func, exp_cli_func = mismatches_ok[pass_name]
            if expected_func != exp_pass_func:
                drift.append(
                    f"_PASS_REGISTRY[{pass_name!r}].runner_func is "
                    f"{expected_func!r}, expected {exp_pass_func!r} per "
                    f"_CLI_RUNNER_FUNC_MISMATCH_OK"
                )
            if runner_func != exp_cli_func:
                drift.append(
                    f"_RUNNERS[{cli_key!r}].function is {runner_func!r}, "
                    f"expected {exp_cli_func!r} per "
                    f"_CLI_RUNNER_FUNC_MISMATCH_OK"
                )
        elif runner_func != expected_func:
            drift.append(
                f"_PASS_REGISTRY[{pass_name!r}].runner_func is "
                f"{expected_func!r} but _RUNNERS[{cli_key!r}].function is "
                f"{runner_func!r} (must match; if intentional, add to "
                f"_CLI_RUNNER_FUNC_MISMATCH_OK)"
            )
    return drift


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------


def collect_drift() -> list[str]:
    """Load the live on-disk surfaces and run every invariant."""

    from robotsix_mill.agents.workflow_portability import _BUILTIN_KINDS
    from robotsix_mill.runtime.routes._passes import _PASS_REGISTRY
    from robotsix_mill.runtime.worker.poll_loops import PollLoopsMixin
    from robotsix_mill.cli import _RUNNERS

    presence_stems = _yaml_stems(_REPO_ROOT / ".robotsix-mill" / "periodic")
    agent_def_stems = _yaml_stems(_REPO_ROOT / "agent_definitions" / "periodic")

    drift: list[str] = []
    drift += check_presence_files_in_builtin(presence_stems, _BUILTIN_KINDS)
    drift += check_agent_defs_in_builtin(
        agent_def_stems, _BUILTIN_KINDS, _AGENT_DEF_GLOBAL_ONLY
    )
    drift += check_pass_registry_vs_builtin(
        _PASS_REGISTRY, _BUILTIN_KINDS, _PASS_KIND_MISMATCH_OK
    )
    drift += check_builtin_llm_agents_have_def(_BUILTIN_KINDS, agent_def_stems)
    drift += check_schedule_only_runner_wiring(
        _BUILTIN_KINDS, PollLoopsMixin._SCHEDULE_ONLY_RUNNERS
    )
    drift += check_pass_registry_cli_runners(
        _PASS_REGISTRY, _RUNNERS, _CLI_RUNNER_FUNC_MISMATCH_OK
    )
    return drift


def main() -> int:
    drift = collect_drift()
    if drift:
        for entry in drift:
            print(f"STALE: {entry}", file=sys.stderr)
        print(
            f"FAIL: {len(drift)} _BUILTIN_KINDS drift item(s) detected",
            file=sys.stderr,
        )
        return 1

    print("_BUILTIN_KINDS OK (all sync points consistent)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
