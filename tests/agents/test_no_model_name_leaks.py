"""Guard: no concrete model IDs leak outside llmio into mill's src/.

The standing rule is that a consumer picks a capability *level* and
llmio owns which provider and model serves it.  Hard-coding a model
slug in mill couples the two repos and silently keeps pointing at the
old model after a swap.

This test scans every ``.py`` file under ``src/`` for forbidden
model-ID patterns.  Provider *prefixes* (``claudeSDK``, ``openrouter``)
are llmio's public identifier vocabulary and are explicitly allowed —
see the ``_ALLOWED_RE`` pattern.
"""

import re
from pathlib import Path

import pytest

# Concrete model-ID substrings that must not appear outside llmio.
# Each pattern matches a model-family prefix/suffix that is only
# meaningful as a specific model binding.
_FORBIDDEN_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"deepseek/"),  # deepseek/deepseek-v4-flash etc.
    re.compile(r"mimo-"),  # xiaomi mimo-pro etc.
    re.compile(r"-opus\b"),  # claude-opus, claude-opus-5 etc.
    re.compile(r"claude-fable"),  # claude-fable-5 etc.
    re.compile(r"\bgpt-"),  # gpt-4o etc.
]

# Patterns that look like model IDs but are allowed:
# - Provider prefixes in string literals (e.g. _CLAUDE_SDK_PROVIDER = "claudeSDK")
# - Historical references in comments/docs about removed detectors
# - Langfuse API examples referencing model formats
_ALLOWED_RE = re.compile(
    r"claudeSDK"  # provider prefix constant
    r"|openrouter"  # provider name
    r"|is_deepseek_reasoning_roundtrip_error"  # removed function name reference
    r"|openai/gpt-4o"  # langfuse API example
)

_SRC = Path("src/robotsix_mill")


def _iter_python_files():
    """Yield all .py files under src/, skipping __pycache__."""
    for p in sorted(_SRC.rglob("*.py")):
        if "__pycache__" in str(p):
            continue
        yield p


@pytest.mark.parametrize(
    "pattern",
    _FORBIDDEN_PATTERNS,
    ids=lambda p: p.pattern,
)
def test_no_model_id_leaks_in_src(pattern: re.Pattern[str]):
    """Assert *pattern* does not appear in any src/ .py file (excluding allowed)."""
    violations: list[str] = []
    for path in _iter_python_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            # Skip comments that are documenting removed code
            stripped = line.strip()
            if stripped.startswith("#"):
                # Allow historical/documentary comments
                if _ALLOWED_RE.search(line):
                    continue
            if pattern.search(line):
                if _ALLOWED_RE.search(line):
                    continue
                violations.append(f"{path}:{lineno}: {line.rstrip()}")

    assert not violations, (
        f"Concrete model-ID pattern {pattern.pattern!r} found in src/:\n"
        + "\n".join(violations)
    )
