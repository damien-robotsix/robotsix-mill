"""reference_files.json must stay paths-only across every finalize path.

2026-10-23 15:49Z: a blocked-path ``_finalize`` re-wrapped the already-loaded
``[{"path": …}]`` list, wrote ``{"path": {"path": "…"}}`` and the next
implement spawn crashed before the agent ran (ticket f611, trace 968f1327).
"""

from robotsix_mill.stages.implement.phase_coordinator import (
    _normalize_reference_files,
)


def test_plain_paths_are_wrapped():
    assert _normalize_reference_files(["src/a.py", "tests/b.py"]) == [
        {"path": "src/a.py"},
        {"path": "tests/b.py"},
    ]


def test_loaded_dicts_are_not_rewrapped():
    loaded = [{"path": "src/a.py"}]
    assert _normalize_reference_files(loaded) == [{"path": "src/a.py"}]


def test_nested_corruption_is_unwrapped():
    corrupt = [{"path": {"path": "src/a.py"}}, {"path": {"path": {"path": "b.py"}}}]
    assert _normalize_reference_files(corrupt) == [
        {"path": "src/a.py"},
        {"path": "b.py"},
    ]


def test_garbage_entries_are_dropped():
    assert _normalize_reference_files([None, 3, {"nope": 1}, "", {"path": ""}]) == []
    assert _normalize_reference_files(None) == []
