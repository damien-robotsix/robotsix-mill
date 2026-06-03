"""Tests for deep_merge, read_yaml_file, load_yaml_cascade."""

from __future__ import annotations

import pytest

from robotsix_yaml_config import (
    YamlConfigError,
    deep_merge,
    load_yaml_cascade,
    read_yaml_file,
)


# ---------------------------------------------------------------------------
#  deep_merge
# ---------------------------------------------------------------------------


def test_deep_merge_scalar_overwrite():
    base = {"a": 1, "b": 2}
    deep_merge(base, {"b": 3})
    assert base == {"a": 1, "b": 3}


def test_deep_merge_nested_recursion():
    base = {"x": {"a": 1, "b": 2}}
    deep_merge(base, {"x": {"b": 20, "c": 30}})
    assert base == {"x": {"a": 1, "b": 20, "c": 30}}


def test_deep_merge_list_replaced_not_extended():
    base = {"items": [1, 2, 3]}
    deep_merge(base, {"items": [4]})
    assert base == {"items": [4]}


def test_deep_merge_mutates_base_and_returns_identity():
    base = {"a": 1}
    result = deep_merge(base, {"b": 2})
    assert result is base
    assert base == {"a": 1, "b": 2}


def test_deep_merge_deepcopies_overlay_values():
    overlay = {"nested": {"k": "v"}}
    base: dict = {}
    deep_merge(base, overlay)
    overlay["nested"]["k"] = "changed"
    assert base == {"nested": {"k": "v"}}


def test_deep_merge_dict_replaces_non_dict_base():
    base = {"a": 5}
    deep_merge(base, {"a": {"nested": 1}})
    assert base == {"a": {"nested": 1}}


# ---------------------------------------------------------------------------
#  read_yaml_file
# ---------------------------------------------------------------------------


def test_read_yaml_file_missing_returns_empty(tmp_path):
    assert read_yaml_file(tmp_path / "nope.yaml") == {}


def test_read_yaml_file_empty_returns_empty(tmp_path):
    p = tmp_path / "empty.yaml"
    p.write_text("", encoding="utf-8")
    assert read_yaml_file(p) == {}


def test_read_yaml_file_none_content_returns_empty(tmp_path):
    p = tmp_path / "comment.yaml"
    p.write_text("# just a comment\n", encoding="utf-8")
    assert read_yaml_file(p) == {}


def test_read_yaml_file_basic_mapping(tmp_path):
    p = tmp_path / "ok.yaml"
    p.write_text("a: 1\nb:\n  c: 2\n", encoding="utf-8")
    assert read_yaml_file(p) == {"a": 1, "b": {"c": 2}}


def test_read_yaml_file_parse_error(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("a: [1, 2\n", encoding="utf-8")
    with pytest.raises(YamlConfigError, match="YAML parse error"):
        read_yaml_file(p)


def test_read_yaml_file_non_dict_list(tmp_path):
    p = tmp_path / "list.yaml"
    p.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(YamlConfigError, match="Expected a mapping"):
        read_yaml_file(p)


def test_read_yaml_file_non_dict_scalar(tmp_path):
    p = tmp_path / "scalar.yaml"
    p.write_text("42\n", encoding="utf-8")
    with pytest.raises(YamlConfigError, match="Expected a mapping"):
        read_yaml_file(p)


# ---------------------------------------------------------------------------
#  load_yaml_cascade
# ---------------------------------------------------------------------------


def test_cascade_required_missing_raises(tmp_path):
    with pytest.raises(YamlConfigError, match="Required config file not found"):
        load_yaml_cascade([(tmp_path / "missing.yaml", True)])


def test_cascade_optional_missing_skipped(tmp_path):
    present = tmp_path / "present.yaml"
    present.write_text("a: 1\n", encoding="utf-8")
    merged = load_yaml_cascade(
        [
            (present, True),
            (tmp_path / "absent.yaml", False),
        ]
    )
    assert merged == {"a": 1}


def test_cascade_merge_order_later_wins(tmp_path):
    f1 = tmp_path / "1.yaml"
    f1.write_text("core:\n  a: 1\n  b: 2\n", encoding="utf-8")
    f2 = tmp_path / "2.yaml"
    f2.write_text("core:\n  b: 20\n  c: 30\n", encoding="utf-8")
    merged = load_yaml_cascade([(f1, True), (f2, False)])
    assert merged == {"core": {"a": 1, "b": 20, "c": 30}}


def test_cascade_empty_layers_returns_empty():
    assert load_yaml_cascade([]) == {}
