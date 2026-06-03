"""Tests for the robotsix-modules CLI entry points."""

from __future__ import annotations

from pathlib import Path

import pytest

from robotsix_modules import SCHEMA_PATH
from robotsix_modules.cli import main, validate_main

FIXTURES = Path(__file__).parent / "fixtures"
VALID = str(FIXTURES / "valid_modules.yaml")
INVALID = str(FIXTURES / "invalid_modules.yaml")


def test_valid_fixture_exit_zero_empty_output(capsys) -> None:
    code = main(["validate", VALID])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == ""
    assert captured.err == ""


def test_invalid_fixture_exit_one_names_pointer(capsys) -> None:
    code = main(["validate", INVALID])
    captured = capsys.readouterr()
    assert code == 1
    # The first module is missing its id; the second has empty paths.
    assert "modules[0]" in captured.err
    assert "modules[1]" in captured.err
    assert captured.out == ""


def test_missing_path_exit_two_names_file(capsys) -> None:
    code = main(["validate", "does-not-exist.yaml"])
    captured = capsys.readouterr()
    assert code == 2
    assert "file not found" in captured.err
    assert "does-not-exist.yaml" in captured.err


def test_version_exit_zero_stdout(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "robotsix-modules 0.1.0"


def test_schema_override_used(capsys) -> None:
    code = main(["validate", VALID, "--schema", str(SCHEMA_PATH)])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == ""


def test_validate_main_multiple_paths(capsys) -> None:
    # One valid + one invalid -> overall exit 1.
    code = validate_main([VALID, INVALID])
    captured = capsys.readouterr()
    assert code == 1
    assert "modules[0]" in captured.err


def test_validate_main_single_valid(capsys) -> None:
    code = validate_main([VALID])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == ""
