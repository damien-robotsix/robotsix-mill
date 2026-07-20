"""Tests for towncrier fragment generation."""

from pathlib import Path

from robotsix_mill.stages.towncrier import maybe_generate_towncrier_fragment


# -- No pyproject.toml -------------------------------------------------


def test_no_pyproject_returns_false(tmp_path: Path) -> None:
    result = maybe_generate_towncrier_fragment(tmp_path, "TICKET-1", "title")
    assert result is False


# -- Missing [tool.towncrier] section ---------------------------------


def test_no_towncrier_section_returns_false(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.black]\nline-length = 88\n")
    result = maybe_generate_towncrier_fragment(tmp_path, "TICKET-1", "title")
    assert result is False


# -- Malformed TOML ----------------------------------------------------


def test_malformed_toml_returns_false(tmp_path: Path, caplog) -> None:
    (tmp_path / "pyproject.toml").write_text("this is not valid toml {{{")
    result = maybe_generate_towncrier_fragment(tmp_path, "TICKET-1", "title")
    assert result is False
    assert "towncrier: failed to parse" in caplog.text


# -- Valid config, default directory ----------------------------------


def test_default_directory_fragment_created(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.towncrier]\npackage = "myproject"\n'
    )
    result = maybe_generate_towncrier_fragment(tmp_path, "TICKET-1", "Fix bug")
    assert result is True
    fragment = tmp_path / "changes" / "TICKET-1.misc.md"
    assert fragment.is_file()
    assert fragment.read_text() == "Fix bug\n"


# -- Valid config, custom directory -----------------------------------


def test_custom_directory_fragment_created(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.towncrier]\npackage = "myproject"\ndirectory = "news"\n'
    )
    result = maybe_generate_towncrier_fragment(tmp_path, "TICKET-2", "Add feature")
    assert result is True
    fragment = tmp_path / "news" / "TICKET-2.misc.md"
    assert fragment.is_file()
    assert fragment.read_text() == "Add feature\n"


# -- Nested directory creation ----------------------------------------


def test_nested_directory_created(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.towncrier]\npackage = "myproject"\ndirectory = "changelog/fragments"\n'
    )
    result = maybe_generate_towncrier_fragment(tmp_path, "TICKET-3", "Deep nested")
    assert result is True
    fragment = tmp_path / "changelog" / "fragments" / "TICKET-3.misc.md"
    assert fragment.is_file()
    assert fragment.read_text() == "Deep nested\n"


# -- Empty title -------------------------------------------------------


def test_empty_title_fragment_created(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.towncrier]\npackage = "myproject"\n'
    )
    result = maybe_generate_towncrier_fragment(tmp_path, "TICKET-4", "")
    assert result is True
    fragment = tmp_path / "changes" / "TICKET-4.misc.md"
    assert fragment.is_file()
    assert fragment.read_text() == "\n"


# -- Duplicate fragment (existing <id>.*.md) --------------------------


def test_existing_fragment_skips_misc(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.towncrier]\npackage = "myproject"\n'
    )
    changes = tmp_path / "changes"
    changes.mkdir()
    (changes / "TICKET-5.feature.md").write_text("Existing fragment")
    result = maybe_generate_towncrier_fragment(tmp_path, "TICKET-5", "Should not write")
    assert result is False
    assert not (changes / "TICKET-5.misc.md").exists()


# -- OSError on write (read-only directory) ---------------------------


def test_oserror_on_write_returns_false(tmp_path: Path, monkeypatch, caplog) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.towncrier]\npackage = "myproject"\n'
    )
    changes = tmp_path / "changes"
    changes.mkdir()

    def _failing_write_text(*args, **kwargs):
        raise OSError("Permission denied")

    monkeypatch.setattr(Path, "write_text", _failing_write_text)
    result = maybe_generate_towncrier_fragment(tmp_path, "TICKET-6", "Should fail")
    assert result is False
    assert "towncrier: failed to write fragment" in caplog.text


# -- OSError on mkdir -------------------------------------------------


def test_oserror_on_mkdir_returns_false(tmp_path: Path, monkeypatch, caplog) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.towncrier]\npackage = "myproject"\ndirectory = "readonly"\n'
    )

    def _failing_mkdir(*args, **kwargs):
        raise OSError("Read-only filesystem")

    monkeypatch.setattr(Path, "mkdir", _failing_mkdir)
    result = maybe_generate_towncrier_fragment(tmp_path, "TICKET-7", "Should fail")
    assert result is False
    assert "towncrier: failed to write fragment" in caplog.text


# -- [tool] key exists but has no towncrier sub-key -------------------


def test_tool_key_without_towncrier_returns_false(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool]\n[tool.other]\n")
    result = maybe_generate_towncrier_fragment(tmp_path, "TICKET-8", "title")
    assert result is False


# -- tomllib load error (monkeypatched) --------------------------------


def test_tomllib_parse_error_returns_false(tmp_path: Path, monkeypatch, caplog) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.towncrier]\npackage = "myproject"\n'
    )
    import tomllib

    def _failing_loads(*args, **kwargs):
        raise ValueError("simulated parse failure")

    monkeypatch.setattr(tomllib, "loads", _failing_loads)
    result = maybe_generate_towncrier_fragment(tmp_path, "TICKET-9", "Should fail")
    assert result is False
    assert "towncrier: failed to parse" in caplog.text
