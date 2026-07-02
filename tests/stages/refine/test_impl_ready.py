"""Tests for implementation-ready spec detection and validation."""

from pathlib import Path

from robotsix_mill.stages.refine._impl_ready import (
    _build_synthetic_refine_result,
    _extract_file_code_pairs,
    _is_implementation_ready,
    _validate_implementation_ready_spec,
)


# ===========================================================================
# _is_implementation_ready
# ===========================================================================


def test_is_implementation_ready_empty():
    assert not _is_implementation_ready("")


def test_is_implementation_ready_no_code_blocks():
    draft = """## Problem
Fix `src/foo.py`.
## Implementation
Just change the file at `src/foo.py`.
"""
    assert not _is_implementation_ready(draft)


def test_is_implementation_ready_code_block_without_path():
    draft = """## Implementation
```python
print("hello")
```
"""
    # Has a code block but no file path annotation — not implementation-ready
    assert not _is_implementation_ready(draft)


def test_is_implementation_ready_with_file_hint():
    draft = """## Implementation
# File: src/foo.py
```python
print("hello")
```
"""
    assert _is_implementation_ready(draft)


def test_is_implementation_ready_with_file_colon_hint():
    draft = """## Implementation
File: src/bar/baz.py
```yaml
key: value
```
"""
    assert _is_implementation_ready(draft)


def test_is_implementation_ready_with_plain_path():
    draft = """## Implementation
src/app/config.py
```python
DEBUG = True
```
"""
    assert _is_implementation_ready(draft)


def test_is_implementation_ready_with_backtick_path():
    draft = """## Implementation
Edit `src/app/main.py`:
```python
def main():
    pass
```
"""
    assert _is_implementation_ready(draft)


def test_is_implementation_ready_multiple_blocks():
    draft = """## Implementation
# File: src/a.py
```python
x = 1
```

File: src/b.yaml
```yaml
key: val
```
"""
    assert _is_implementation_ready(draft)


def test_is_implementation_ready_with_toml():
    draft = """## Implementation
# File: pyproject.toml
```toml
[tool]
x = 1
```
"""
    assert _is_implementation_ready(draft)


# ===========================================================================
# _extract_file_code_pairs
# ===========================================================================


def test_extract_pairs_empty():
    assert _extract_file_code_pairs("") == []


def test_extract_pairs_single():
    draft = """# File: src/foo.py
```python
print("hello")
```
"""
    pairs = _extract_file_code_pairs(draft)
    assert len(pairs) == 1
    assert pairs[0] == ("src/foo.py", "python", 'print("hello")')


def test_extract_pairs_multiple():
    draft = """# File: src/a.py
```python
x = 1
```

File: src/b.yaml
```yaml
key: val
```
"""
    pairs = _extract_file_code_pairs(draft)
    assert len(pairs) == 2
    assert pairs[0] == ("src/a.py", "python", "x = 1")
    assert pairs[1] == ("src/b.yaml", "yaml", "key: val")


def test_extract_pairs_with_blank_lines():
    draft = """# File: src/foo.py



```python
print("x")
```
"""
    pairs = _extract_file_code_pairs(draft)
    assert len(pairs) == 1
    assert pairs[0][0] == "src/foo.py"


def test_extract_pairs_code_with_trailing_newline():
    draft = """# File: src/foo.py
```python
line1
line2

```
"""
    pairs = _extract_file_code_pairs(draft)
    assert len(pairs) == 1
    # trailing blank line inside code fence (before closing ```) is included
    assert pairs[0][2] == "line1\nline2\n"


# ===========================================================================
# _validate_implementation_ready_spec
# ===========================================================================


def test_validate_empty_draft():
    err = _validate_implementation_ready_spec("", None)
    assert err == "no file-path + code-block pairs found in draft"


def test_validate_valid_yaml(tmp_path: Path):
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "test.yaml").write_text("key: value\n")
    draft = """# File: src/test.yaml
```yaml
key: value
```
"""
    err = _validate_implementation_ready_spec(draft, tmp_path)
    assert err is None


def test_validate_invalid_yaml(tmp_path: Path):
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "test.yaml").write_text("ok")
    draft = """# File: src/test.yaml
```yaml
key: [unclosed
```
"""
    err = _validate_implementation_ready_spec(draft, tmp_path)
    assert err is not None
    assert "invalid YAML" in err


def test_validate_valid_python(tmp_path: Path):
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "test.py").write_text("# existing")
    draft = """# File: src/test.py
```python
x = 1
```
"""
    err = _validate_implementation_ready_spec(draft, tmp_path)
    assert err is None


def test_validate_invalid_python(tmp_path: Path):
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "test.py").write_text("# ok")
    draft = """# File: src/test.py
```python
def broken(
```
"""
    err = _validate_implementation_ready_spec(draft, tmp_path)
    assert err is not None
    assert "invalid Python" in err


def test_validate_missing_file(tmp_path: Path):
    draft = """# File: src/nonexistent.py
```python
print("hi")
```
"""
    err = _validate_implementation_ready_spec(draft, tmp_path)
    assert err is not None
    assert "does not exist" in err


def test_validate_no_repo_dir():
    """When repo_dir is None, file-existence checks are skipped."""
    draft = """# File: src/anything.py
```python
x = 1
```
"""
    err = _validate_implementation_ready_spec(draft, None)
    assert err is None


def test_validate_path_escape(tmp_path: Path):
    (tmp_path / "src").mkdir(parents=True)
    draft = """# File: ../../etc/passwd
```yaml
x: 1
```
"""
    err = _validate_implementation_ready_spec(draft, tmp_path)
    assert err is not None
    assert "escapes repo" in err


def test_validate_forbidden_workflow_call_pattern(tmp_path: Path):
    (tmp_path / ".github").mkdir(parents=True)
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("")
    draft = """# File: .github/workflows/ci.yml
```yaml
on:
  workflow_call:
    inputs:
      image:
        type: string
        default: ${{ inputs.image }}
```
"""
    err = _validate_implementation_ready_spec(draft, tmp_path)
    assert err is not None
    assert "forbidden pattern" in err


# ===========================================================================
# _build_synthetic_refine_result
# ===========================================================================


def test_build_synthetic_refine_result():
    draft = "## Problem\nFix something."
    result = _build_synthetic_refine_result(draft)
    assert result.split is False
    assert result.spec_markdown == draft
    assert result.children is None
    assert result.promote_to_epic is False
    assert result.no_change_needed is False
