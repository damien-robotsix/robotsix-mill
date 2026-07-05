"""Tests for core/constants.py — shared constant definitions."""

from __future__ import annotations

from robotsix_mill.core import constants


class TestNonImplementationClosePrefixes:
    def test_all_prefixes_are_strings(self) -> None:
        for prefix in constants.NON_IMPLEMENTATION_CLOSE_PREFIXES:
            assert isinstance(prefix, str)

    def test_dedup_duplicate_prefix(self) -> None:
        assert constants.DEDUP_DUPLICATE_PREFIX == "duplicate of "

    def test_dedup_already_done_prefix(self) -> None:
        assert constants.DEDUP_ALREADY_DONE_PREFIX == "already implemented in "

    def test_freshness_stale_prefix(self) -> None:
        assert constants.FRESHNESS_STALE_PREFIX == "stale or invalid finding"

    def test_obsolescence_gap_prefix(self) -> None:
        assert constants.OBSOLESCENCE_GAP_PREFIX == "obsolete — gap already resolved"

    def test_refine_mill_misroute_prefix(self) -> None:
        assert constants.REFINE_MILL_MISROUTE_PREFIX == "redirected to mill board"

    def test_refine_mill_consumer_followup_prefix(self) -> None:
        assert (
            constants.REFINE_MILL_CONSUMER_FOLLOWUP_PREFIX
            == "filed mill consumer follow-up"
        )

    def test_tuple_contains_all_individual_prefixes(self) -> None:
        assert constants.DEDUP_DUPLICATE_PREFIX in constants.NON_IMPLEMENTATION_CLOSE_PREFIXES
        assert constants.DEDUP_ALREADY_DONE_PREFIX in constants.NON_IMPLEMENTATION_CLOSE_PREFIXES
        assert constants.FRESHNESS_STALE_PREFIX in constants.NON_IMPLEMENTATION_CLOSE_PREFIXES
        assert constants.OBSOLESCENCE_GAP_PREFIX in constants.NON_IMPLEMENTATION_CLOSE_PREFIXES
        assert constants.REFINE_MILL_MISROUTE_PREFIX in constants.NON_IMPLEMENTATION_CLOSE_PREFIXES


class TestBinaryExtensions:
    def test_is_frozenset(self) -> None:
        assert isinstance(constants.BINARY_EXTENSIONS, frozenset)

    def test_contains_common_binary_extensions(self) -> None:
        assert ".gz" in constants.BINARY_EXTENSIONS
        assert ".zip" in constants.BINARY_EXTENSIONS
        assert ".png" in constants.BINARY_EXTENSIONS
        assert ".pdf" in constants.BINARY_EXTENSIONS
        assert ".pyc" in constants.BINARY_EXTENSIONS
        assert ".so" in constants.BINARY_EXTENSIONS

    def test_does_not_contain_text_extensions(self) -> None:
        assert ".py" not in constants.BINARY_EXTENSIONS
        assert ".md" not in constants.BINARY_EXTENSIONS
        assert ".txt" not in constants.BINARY_EXTENSIONS
        assert ".toml" not in constants.BINARY_EXTENSIONS
        assert ".yml" not in constants.BINARY_EXTENSIONS

    def test_all_extensions_start_with_dot(self) -> None:
        for ext in constants.BINARY_EXTENSIONS:
            assert ext.startswith("."), f"Extension {ext!r} does not start with '.'"
