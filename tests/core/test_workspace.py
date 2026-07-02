"""Unit tests for ``Workspace`` — core path resolution, I/O, and failure modes."""

from __future__ import annotations

import errno
import hashlib
from pathlib import Path

import pytest

from robotsix_mill.core.workspace import Workspace


# ---- constructor ---------------------------------------------------------


def test_constructor_creates_directory(tmp_path: Path) -> None:
    """Workspace(dir) is created on construction."""
    ws = Workspace(tmp_path, "T-1")
    assert ws.dir.is_dir()
    assert ws.dir == tmp_path / "T-1"


def test_constructor_creates_parents(tmp_path: Path) -> None:
    """Missing parent directories are created automatically."""
    ws = Workspace(tmp_path / "a" / "b", "T-2")
    assert ws.dir.is_dir()
    assert ws.dir == tmp_path / "a" / "b" / "T-2"


def test_constructor_idempotent(tmp_path: Path) -> None:
    """Existing workspace directories are reused without error."""
    ws1 = Workspace(tmp_path, "T-3")
    (ws1.dir / "marker").write_text("hello")
    ws2 = Workspace(tmp_path, "T-3")
    assert (ws2.dir / "marker").read_text() == "hello"


def test_constructor_permission_error_raises(tmp_path: Path, monkeypatch) -> None:
    """A permission error during mkdir propagates."""
    from pathlib import Path as PathCls

    def _failing_mkdir(self, mode=0o777, parents=False, exist_ok=False):
        raise PermissionError(errno.EACCES, "Permission denied", str(self))

    monkeypatch.setattr(PathCls, "mkdir", _failing_mkdir, raising=True)
    with pytest.raises(PermissionError):
        Workspace(tmp_path, "T-perm")


@pytest.mark.parametrize(
    "unsafe_id",
    [
        "../escape",
        "sub/dir",
        "..",
        ".",
    ],
)
def test_constructor_rejects_path_traversal(tmp_path: Path, unsafe_id: str) -> None:
    """A ticket_id containing path separators or '.'/'..' raises ValueError."""
    with pytest.raises(ValueError, match="Unsafe ticket_id"):
        Workspace(tmp_path, unsafe_id)


# ---- paths ---------------------------------------------------------------


def test_description_path(tmp_path: Path) -> None:
    """description_path points at description.md inside the workspace."""
    ws = Workspace(tmp_path, "T-4")
    assert ws.description_path == ws.dir / "description.md"


def test_artifacts_dir_created_lazily(tmp_path: Path) -> None:
    """artifacts_dir is created on first access, not on construction."""
    ws = Workspace(tmp_path, "T-5")
    # artifacts/ must NOT exist after construction
    assert not (ws.dir / "artifacts").exists()
    d = ws.artifacts_dir
    assert d == ws.dir / "artifacts"
    assert d.is_dir()


def test_artifacts_dir_idempotent(tmp_path: Path) -> None:
    """Repeated access to artifacts_dir does not fail."""
    ws = Workspace(tmp_path, "T-6")
    # Access twice to confirm second access is a no-op
    d1 = ws.artifacts_dir
    d2 = ws.artifacts_dir
    assert d1 == d2
    (d1 / "out.log").write_text("log")
    assert (d1 / "out.log").read_text() == "log"


def test_repo_dir_path_no_creation(tmp_path: Path) -> None:
    """repo_dir returns the correct path but does NOT create the directory."""
    ws = Workspace(tmp_path, "T-7")
    assert ws.repo_dir == ws.dir / "repo"
    assert not ws.repo_dir.exists()


# ---- write_description ---------------------------------------------------


def test_write_description_returns_sha256_hex(tmp_path: Path) -> None:
    """write_description returns the lowercase SHA-256 hex digest of the file."""
    ws = Workspace(tmp_path, "T-8")
    h = ws.write_description("hello")
    expected = hashlib.sha256(b"hello").hexdigest()
    assert h == expected
    assert len(h) == 64


def test_write_description_writes_utf8(tmp_path: Path) -> None:
    """write_description writes the text to description.md as UTF-8."""
    ws = Workspace(tmp_path, "T-9")
    ws.write_description("café")
    assert ws.description_path.read_text(encoding="utf-8") == "café"


def test_write_description_overwrites(tmp_path: Path) -> None:
    """write_description overwrites existing content and returns the new hash."""
    ws = Workspace(tmp_path, "T-10")
    ws.write_description("first")
    h2 = ws.write_description("second")
    assert ws.description_path.read_text(encoding="utf-8") == "second"
    assert h2 == hashlib.sha256(b"second").hexdigest()


def test_write_description_empty_string(tmp_path: Path) -> None:
    """write_description accepts an empty string."""
    ws = Workspace(tmp_path, "T-11")
    h = ws.write_description("")
    assert ws.description_path.read_text(encoding="utf-8") == ""
    assert h == hashlib.sha256(b"").hexdigest()


def test_write_description_permission_error_raises(tmp_path: Path, monkeypatch) -> None:
    """A permission error during file write propagates."""
    from pathlib import Path as PathCls

    ws = Workspace(tmp_path, "T-12")

    def _failing_write_text(self, data, encoding=None, errors=None, newline=None):
        raise PermissionError(errno.EACCES, "Permission denied", str(self))

    monkeypatch.setattr(PathCls, "write_text", _failing_write_text, raising=True)
    with pytest.raises(PermissionError):
        ws.write_description("boom")


# ---- read_description ----------------------------------------------------


def test_read_description_returns_content(tmp_path: Path) -> None:
    """read_description returns the text content of description.md."""
    ws = Workspace(tmp_path, "T-13")
    ws.description_path.write_text("the body", encoding="utf-8")
    assert ws.read_description() == "the body"


def test_read_description_empty_when_absent(tmp_path: Path) -> None:
    """read_description returns an empty string when description.md does not exist."""
    ws = Workspace(tmp_path, "T-14")
    assert ws.read_description() == ""


def test_read_description_empty_file(tmp_path: Path) -> None:
    """read_description returns an empty string for an empty description.md."""
    ws = Workspace(tmp_path, "T-15")
    ws.description_path.write_text("", encoding="utf-8")
    assert ws.read_description() == ""


def test_read_description_utf8(tmp_path: Path) -> None:
    """read_description reads UTF-8 content correctly."""
    ws = Workspace(tmp_path, "T-16")
    ws.description_path.write_text("zażółć gęślą jaźń", encoding="utf-8")
    assert ws.read_description() == "zażółć gęślą jaźń"


# ---- content_hash --------------------------------------------------------


def test_content_hash_sha256_hex(tmp_path: Path) -> None:
    """content_hash returns the SHA-256 hex digest of description.md."""
    ws = Workspace(tmp_path, "T-17")
    ws.description_path.write_bytes(b"payload")
    expected = hashlib.sha256(b"payload").hexdigest()
    assert ws.content_hash() == expected


def test_content_hash_empty_when_absent(tmp_path: Path) -> None:
    """content_hash returns an empty string when description.md does not exist."""
    ws = Workspace(tmp_path, "T-18")
    assert ws.content_hash() == ""


def test_content_hash_changes_with_content(tmp_path: Path) -> None:
    """content_hash reflects content changes."""
    ws = Workspace(tmp_path, "T-19")
    ws.description_path.write_text("v1", encoding="utf-8")
    h1 = ws.content_hash()
    ws.description_path.write_text("v2", encoding="utf-8")
    h2 = ws.content_hash()
    assert h1 != h2


def test_content_hash_deterministic(tmp_path: Path) -> None:
    """content_hash is deterministic for identical content."""
    ws = Workspace(tmp_path, "T-20")
    ws.description_path.write_text("same", encoding="utf-8")
    assert ws.content_hash() == ws.content_hash()
