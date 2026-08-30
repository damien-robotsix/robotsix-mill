"""Deterministic semantic bucketing of CI failures.

The ci-fix stage records a ``CI_FAILURE`` diagnostic event every time it
confirms CI is red. The event's ``normalized_key`` is a hash of the failing
check names — in practice one key per check (``ci / tests``), which says
nothing about *why* CI failed. This module derives a small, stable
vocabulary of semantic ``bucket`` values (``ruff-format``, ``mypy``,
``pytest-failure``, …) from the failing check names plus the job-log
excerpt, along with a one-line ``root_cause`` and a default
``prevention_rule`` per bucket.

The ``ci_prevention_rules`` periodic pass groups events by bucket and
distils the recurring ones into imperative rules that are injected into the
implement agent's memory ledger — so the same class of failure stops
happening upstream instead of being fixed downstream by ci_fix.

Everything here is pure string matching: no LLM, no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Semantic bucket names. Kept as a frozenset so tests and the pass can
# validate that a stored bucket is a known one.
BUCKETS: frozenset[str] = frozenset(
    {
        "ruff-format",
        "ruff-lint",
        "mypy",
        "pytest-failure",
        "pytest-collection/import",
        "modules-yaml-unregistered",
        "vulture",
        "deptry",
        "codeql",
        "trivy",
        "flaky-network",
        "unknown",
    }
)

# Default prevention rule per bucket — the deterministic seed the LLM pass
# refines. Imperative, one line, tool-agnostic where the repo may differ.
DEFAULT_PREVENTION_RULES: dict[str, str] = {
    "ruff-format": (
        "Run the formatter (`ruff format`) on every changed file before stopping."
    ),
    "ruff-lint": (
        "Run the linter (`ruff check --fix`) on changed files and fix every "
        "remaining finding before stopping."
    ),
    "mypy": (
        "Run the type checker on changed modules and fix new errors before "
        "stopping; do not rely on CI to find them."
    ),
    "pytest-failure": (
        "Run the full test suite locally before stopping and fix or update "
        "the tests your change broke."
    ),
    "pytest-collection/import": (
        "After moving, renaming or deleting a symbol, grep for every import of "
        "it and run test collection (`pytest --collect-only`) before stopping."
    ),
    "modules-yaml-unregistered": (
        "Register every new file (and de-register every deleted file) in the "
        "module registry in the same commit."
    ),
    "vulture": (
        "Remove dead code you introduce or whitelist it deliberately; run the "
        "dead-code check before stopping."
    ),
    "deptry": (
        "Declare every new import's distribution in the project dependencies "
        "and remove dependencies that are no longer imported."
    ),
    "codeql": (
        "Avoid the patterns CodeQL flags (unsanitised input, clear-text "
        "logging of secrets, path traversal); check alerts before pushing."
    ),
    "trivy": (
        "Do not add dependencies or base images with known CVEs; check the "
        "vulnerability scanner's output before pushing."
    ),
    "flaky-network": (
        "Treat network/infrastructure failures as transient — re-run the "
        "check rather than changing code."
    ),
    "unknown": "",
}


@dataclass(frozen=True, slots=True)
class CIFailureClass:
    """A bucketed CI failure: bucket, one-line root cause, prevention rule."""

    bucket: str
    root_cause: str
    prevention_rule: str


# Ordered (bucket, patterns) table. The first bucket whose pattern matches
# the lower-cased haystack wins, so the most specific tool signatures come
# first and the generic pytest / network / unknown fallbacks come last.
_BUCKET_PATTERNS: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    (
        "ruff-format",
        (
            re.compile(r"ruff format"),
            re.compile(r"would reformat"),
            re.compile(r"\d+ files? would be reformatted"),
            re.compile(r"ruff-format"),
        ),
    ),
    (
        "ruff-lint",
        (
            re.compile(r"ruff check"),
            re.compile(r"ruff\b.*\bfound \d+ error"),
            # Rule codes as ruff prints them: "F401 [*] `os` imported but unused".
            re.compile(r"\b(?:f|e|w|i|b|up|c|n|sim|ret|s|ruf|pl[cerw])\d{3}\b"),
            re.compile(r"imported but unused"),
        ),
    ),
    (
        "mypy",
        (
            re.compile(r"\bmypy\b"),
            re.compile(r"error: incompatible (?:types|return value|default)"),
            re.compile(
                r"\[(?:arg-type|assignment|return-value|attr-defined|"
                r"union-attr|no-untyped-def|call-arg|misc|override|"
                r"index|operator|var-annotated|type-arg|no-any-return|"
                r"name-defined|import-not-found|import-untyped|unused-ignore)\]"
            ),
        ),
    ),
    (
        "vulture",
        (
            re.compile(r"\bvulture\b"),
            re.compile(
                r"unused (?:function|variable|class|import|attribute|method|property) '"
            ),
        ),
    ),
    (
        "deptry",
        (
            re.compile(r"\bdeptry\b"),
            re.compile(r"\bdep00[1-4]\b"),
        ),
    ),
    (
        "modules-yaml-unregistered",
        (
            re.compile(r"modules\.yaml"),
            re.compile(r"check-registration"),
            re.compile(r"unregistered file"),
            re.compile(r"not (?:claimed|registered) by any module"),
        ),
    ),
    (
        "codeql",
        (
            re.compile(r"\bcodeql\b"),
            re.compile(r"code[- ]scanning"),
        ),
    ),
    (
        "trivy",
        (
            re.compile(r"\btrivy\b"),
            re.compile(r"\bcve-\d{4}-\d+"),
        ),
    ),
    (
        "pytest-collection/import",
        (
            re.compile(r"modulenotfounderror"),
            re.compile(r"\bimporterror\b"),
            re.compile(r"cannot import name"),
            re.compile(r"error(?:s)? (?:during|while) collect"),
            re.compile(r"errors during collection"),
            re.compile(r"interrupted: \d+ errors?"),
            re.compile(r"collected 0 items"),
        ),
    ),
    (
        "pytest-failure",
        (
            re.compile(r"\bfailed tests?/"),
            re.compile(r"\d+ failed\b"),
            re.compile(r"\bassertionerror\b"),
            re.compile(r"=+ failures =+"),
            re.compile(r"\bpytest\b"),
        ),
    ),
    (
        "flaky-network",
        (
            re.compile(r"econnreset"),
            re.compile(r"connection reset by peer"),
            re.compile(r"temporary failure in name resolution"),
            re.compile(r"network is unreachable"),
            re.compile(r"failed to fetch"),
            re.compile(
                r"\b50[234] (?:service unavailable|bad gateway|gateway time-?out)"
            ),
            re.compile(r"the runner has received a shutdown signal"),
            re.compile(r"the operation was canceled"),
            re.compile(r"connection timed out"),
            re.compile(r"read timed out"),
        ),
    ),
)

# Log lines carrying the actual error message — used to pick a one-line
# root cause. Tried in order; the first matching line wins.
_ROOT_CAUSE_LINE_RE: tuple[re.Pattern[str], ...] = (
    re.compile(r"^.*(?:error|Error|ERROR)[:\]].*$", re.MULTILINE),
    re.compile(r"^FAILED .*$", re.MULTILINE),
    re.compile(r"^.*would reformat.*$", re.MULTILINE),
    re.compile(r"^.*(?:unused|missing|unregistered).*$", re.MULTILINE),
)

_MAX_ROOT_CAUSE_CHARS = 240


def _check_names(failing: list[dict[str, Any]]) -> list[str]:
    return [str(chk.get("name") or "").strip() for chk in failing if chk]


def _haystack(failing: list[dict[str, Any]], failing_summary: str) -> str:
    names = " ".join(_check_names(failing))
    summaries = " ".join(str(chk.get("summary") or "") for chk in failing if chk)
    return f"{names}\n{summaries}\n{failing_summary}".lower()


def _pick_root_cause(failing_summary: str, bucket: str, names: list[str]) -> str:
    """Pick the most informative single line from the summary/log.

    Falls back to ``"<bucket>: <check names>"`` when nothing looks like an
    error line — the root cause must never be empty for a known bucket.
    """
    text = failing_summary or ""
    # Skip the markdown scaffolding (## headers, **bold** labels) the
    # summary builder adds; those are never the error line.
    for pattern in _ROOT_CAUSE_LINE_RE:
        for m in pattern.finditer(text):
            line = m.group(0).strip()
            if not line or line.startswith(("#", "**", "```", "- [")):
                continue
            # Strip a leading GitHub-annotation prefix like "[error] path:12: ".
            line = re.sub(r"^\[\w+\]\s*", "", line)
            return line[:_MAX_ROOT_CAUSE_CHARS]
    joined = ", ".join(n for n in names if n) or "(unknown check)"
    return f"{bucket}: {joined}"[:_MAX_ROOT_CAUSE_CHARS]


def classify_ci_failure(
    failing: list[dict[str, Any]], failing_summary: str = ""
) -> CIFailureClass:
    """Bucket a CI failure from its failing checks and summary/log excerpt.

    Deterministic: the same inputs always yield the same
    :class:`CIFailureClass`. Unknown failure modes bucket as ``"unknown"``
    with an empty prevention rule — the LLM pass may still read the root
    cause but has nothing deterministic to seed from.
    """
    hay = _haystack(failing, failing_summary)
    bucket = "unknown"
    for name, patterns in _BUCKET_PATTERNS:
        if any(p.search(hay) for p in patterns):
            bucket = name
            break
    names = _check_names(failing)
    return CIFailureClass(
        bucket=bucket,
        root_cause=_pick_root_cause(failing_summary, bucket, names),
        prevention_rule=DEFAULT_PREVENTION_RULES.get(bucket, ""),
    )
