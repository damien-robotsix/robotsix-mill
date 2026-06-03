"""Tests for flatten_config."""

from __future__ import annotations

from robotsix_yaml_config import flatten_config


def test_flatten_basic_nesting():
    nested = {"core": {"models": {"coordinator": "deepseek/x"}}}
    alias_map = {"core.models.coordinator": "model"}
    assert flatten_config(nested, alias_map) == {"model": "deepseek/x"}


def test_flatten_unknown_path_dropped():
    nested = {"core": {"unknown": 1, "known": 2}}
    alias_map = {"core.known": "known_alias"}
    assert flatten_config(nested, alias_map) == {"known_alias": 2}


def test_flatten_dict_valued_alias_emitted_as_is():
    nested = {"core": {"limits": {"overrides": {"refine": 10, "test": 20}}}}
    alias_map = {"core.limits.overrides": "stage_timeout_overrides"}
    result = flatten_config(nested, alias_map)
    assert result == {"stage_timeout_overrides": {"refine": 10, "test": 20}}


def test_flatten_no_recursion_into_matched_dict():
    # The alias matches the dict node; leaves inside have their own
    # alias entries but must NOT be emitted because descent stops.
    nested = {"a": {"b": {"c": 1}}}
    alias_map = {"a.b": "ab", "a.b.c": "abc"}
    result = flatten_config(nested, alias_map)
    assert result == {"ab": {"c": 1}}
    assert "abc" not in result


def test_flatten_duplicate_alias_last_wins():
    nested = {
        "core": {"models": {"web_research": "first"}},
        "web": {"research_model": "second"},
    }
    alias_map = {
        "core.models.web_research": "web_research_model",
        "web.research_model": "web_research_model",
    }
    result = flatten_config(nested, alias_map)
    assert result == {"web_research_model": "second"}


def test_flatten_empty_inputs():
    assert flatten_config({}, {}) == {}
    assert flatten_config({"a": 1}, {}) == {}
