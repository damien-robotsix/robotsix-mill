"""CI prevention-rules runner — per-board, daily.

Two phases, mirroring ``run_health_runner``:

    Phase 1 (deterministic, no LLM):
        Read the board's most recent ``CI_FAILURE`` diagnostic events (the
        ci-fix stage emits one per confirmed red CI, bucketed by failure
        class) plus the ``CI_FIX_RESOLVED`` events that pair with them, and
        render a per-bucket ``<ci-failure-digest>`` block: counts, distinct
        tickets, resolution rate, sample root causes, the bucket's default
        prevention rule and how the ci-fix agent said it fixed them.

    Phase 2 (LLM, small tier):
        Hand the digest to the ``ci_prevention_rules`` agent, which returns
        at most ``settings.ci_prevention_max_rules`` short imperative rules.

The runner then REWRITES the ``## CI prevention rules (auto-maintained)``
section at the top of the board's implement memory ledger
(``settings.memory_file_for("implement", board_id)``) in place — never
appending, never accreting, removing the section outright when there are
no rules — and preserves every other byte of the ledger. It files no
tickets.

Seam: tests monkeypatch ``run_ci_prevention_rules_agent``.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...config import RepoConfig, Settings
from ..ci_prevention_rules import (
    CiPreventionRulesResult,
    run_ci_prevention_rules_agent,
)
from ..prompt_blocks import section
from .diagnostic_events import DiagnosticEvent, list_diagnostic_events

log = logging.getLogger("robotsix_mill.ci_prevention_rules")

SECTION_HEADING = "## CI prevention rules (auto-maintained)"
SECTION_END_MARKER = "<!-- /ci-prevention-rules -->"
_SECTION_INTRO = (
    "Rewritten daily by the `ci_prevention_rules` pass from this board's "
    "recent CI failures. Do not edit by hand — edits are overwritten. Follow "
    "these before you stop:"
)

_MAX_SAMPLES_PER_BUCKET = 3
_MAX_SAMPLE_CHARS = 200
_MAX_RULE_CHARS = 300


@dataclass
class CiPreventionRulesPassResult:
    """Result of one ci_prevention_rules pass.

    ``drafts_created`` is always empty — the pass files no tickets — but the
    shared poll loop reads it, so it stays on the result.
    """

    drafts_created: list[dict[str, Any]] = field(default_factory=list)
    session_id: str = ""
    summary: str = ""
    rules_written: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Ledger section rewrite (pure text functions)
# ---------------------------------------------------------------------------


def render_section(rules: list[str]) -> str:
    """Render the auto-maintained section for *rules* (non-empty)."""
    bullets = "\n".join(f"- {rule}" for rule in rules)
    return (
        f"{SECTION_HEADING}\n\n{_SECTION_INTRO}\n\n{bullets}\n\n{SECTION_END_MARKER}\n"
    )


def _find_section(text: str) -> tuple[int, int] | None:
    """Return ``(start, end)`` of the auto-maintained section in *text*.

    ``end`` is exclusive and includes the end marker's newline plus the one
    blank line :func:`upsert_section` inserts after it. Falls back to the
    next level-2 heading (or EOF) when the end marker was hand-deleted.
    """
    start = text.find(SECTION_HEADING)
    while start > 0 and text[start - 1] != "\n":
        # The heading matched mid-line (quoted in prose); keep looking.
        start = text.find(SECTION_HEADING, start + 1)
    if start == -1:
        return None
    marker = text.find(SECTION_END_MARKER, start)
    if marker != -1:
        end = marker + len(SECTION_END_MARKER)
        if text.startswith("\n", end):
            end += 1
    else:
        nxt = text.find("\n## ", start + len(SECTION_HEADING))
        end = len(text) if nxt == -1 else nxt + 1
    # Consume the single separator newline the upsert added after the block.
    if text.startswith("\n", end):
        end += 1
    return start, end


def remove_section(text: str) -> str:
    """Return *text* without the auto-maintained section (byte-preserving)."""
    span = _find_section(text)
    if span is None:
        return text
    start, end = span
    return text[:start] + text[end:]


def upsert_section(text: str, rules: list[str]) -> str:
    """Rewrite the auto-maintained section at the TOP of *text* in place.

    Every byte outside the section is preserved. An empty *rules* list
    removes the section. Idempotent: applying the same rules twice yields
    the same document.
    """
    rest = remove_section(text)
    if not rules:
        return rest
    block = render_section(rules)
    return block + ("\n" + rest if rest else "")


def write_rules_to_ledger(ledger: Path, rules: list[str]) -> bool:
    """Upsert *rules* into the ledger file; return ``True`` when it changed.

    A missing ledger is created only when there is something to write.
    """
    try:
        current = ledger.read_text(encoding="utf-8") if ledger.exists() else ""
    except OSError:
        log.warning("ci_prevention_rules: could not read ledger %s", ledger)
        current = ""
    updated = upsert_section(current, rules)
    if updated == current:
        return False
    if not updated and not ledger.exists():
        return False
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(updated, encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Phase 1 — deterministic digest
# ---------------------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _unique(values: list[str], limit: int, max_chars: int) -> list[str]:
    seen: list[str] = []
    for value in values:
        clipped = _clip(value, max_chars)
        if clipped and clipped not in seen:
            seen.append(clipped)
        if len(seen) >= limit:
            break
    return seen


def build_digest(
    failures: list[DiagnosticEvent],
    resolved: list[DiagnosticEvent],
) -> str:
    """Render the per-bucket ``<ci-failure-digest>`` block."""
    if not failures:
        return section("ci-failure-digest", "(no CI_FAILURE events in the window)")

    by_bucket: dict[str, list[DiagnosticEvent]] = defaultdict(list)
    for ev in failures:
        by_bucket[ev.bucket or "unknown"].append(ev)
    resolved_by_pair: dict[tuple[str, str], list[DiagnosticEvent]] = defaultdict(list)
    for ev in resolved:
        resolved_by_pair[(ev.ticket_id, ev.normalized_key)].append(ev)

    def _weight(item: tuple[str, list[DiagnosticEvent]]) -> tuple[int, str]:
        _bucket, evs = item
        return (-(len(evs) * len({e.ticket_id for e in evs})), _bucket)

    header = (
        f"{len(failures)} CI_FAILURE event(s) across "
        f"{len({e.ticket_id for e in failures})} ticket(s), "
        f"{len(by_bucket)} bucket(s). Most impactful first."
    )
    lines = [header, ""]
    for bucket, evs in sorted(by_bucket.items(), key=_weight):
        tickets = {e.ticket_id for e in evs}
        fixes = [
            r
            for e in evs
            for r in resolved_by_pair.get((e.ticket_id, e.normalized_key), [])
        ]
        default_rule = next((e.prevention_rule for e in evs if e.prevention_rule), "")
        lines.append(f"### {bucket}")
        lines.append(
            f"- failures: {len(evs)} | distinct tickets: {len(tickets)} | "
            f"resolved by ci_fix: {len(fixes)}"
        )
        lines.append(f"- default rule: {default_rule or '(none)'}")
        causes = _unique(
            [e.root_cause or e.reason for e in reversed(evs)],
            _MAX_SAMPLES_PER_BUCKET,
            _MAX_SAMPLE_CHARS,
        )
        if causes:
            lines.append("- sample root causes:")
            lines.extend(f"  - {c}" for c in causes)
        approaches = _unique(
            [r.prevention_rule for r in fixes] + [r.root_cause for r in fixes],
            _MAX_SAMPLES_PER_BUCKET,
            _MAX_SAMPLE_CHARS,
        )
        if approaches:
            lines.append("- how ci_fix fixed them:")
            lines.extend(f"  - {a}" for a in approaches)
        lines.append("")
    return section("ci-failure-digest", "\n".join(lines).rstrip())


def _clean_rules(rules: list[str], max_rules: int) -> list[str]:
    out: list[str] = []
    for rule in rules:
        text = " ".join(str(rule).split()).lstrip("-• ").strip()
        if not text:
            continue
        text = text[:_MAX_RULE_CHARS]
        if text.casefold() not in {r.casefold() for r in out}:
            out.append(text)
        if len(out) >= max_rules:
            break
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_ci_prevention_rules_pass(
    session_id: str,
    repo_config: RepoConfig | None = None,
    *,
    definition_override: Any = None,
) -> CiPreventionRulesPassResult:
    """Execute one ci_prevention_rules pass for *repo_config*'s board.

    Args:
        session_id: Langfuse session id from the poll loop.
        repo_config: Per-repo configuration. Required — the ledger and the
            event store are both per-board.
        definition_override: The merged periodic definition when dispatched
            from a presence file; the built-in YAML otherwise.
    """
    settings = Settings()
    if repo_config is None:
        raise ValueError(
            "run_ci_prevention_rules_pass: repo_config is required — the "
            "implement ledger and the CI event store are per-board."
        )
    board_id = repo_config.board_id
    ledger = settings.memory_file_for("implement", board_id)

    failures = list_diagnostic_events(settings, board_id, category="CI_FAILURE")
    failures = failures[-settings.ci_prevention_rules_max_events :]
    resolved = list_diagnostic_events(settings, board_id, category="CI_FIX_RESOLVED")

    if not failures:
        changed = write_rules_to_ledger(ledger, [])
        return CiPreventionRulesPassResult(
            session_id=session_id,
            summary=(
                "no CI_FAILURE events; prevention-rules section "
                + ("removed" if changed else "absent")
            ),
        )

    if definition_override is not None:
        definition = definition_override
    else:
        from ..._resources import agent_definitions_dir
        from ..yaml_loader import load_agent_definition

        definition = load_agent_definition(
            agent_definitions_dir() / "periodic" / "ci_prevention_rules.yaml"
        )

    digest = build_digest(failures, resolved)
    result: CiPreventionRulesResult = run_ci_prevention_rules_agent(
        settings=settings, digest=digest, definition=definition
    )
    rules = _clean_rules(result.rules, settings.ci_prevention_max_rules)

    changed = write_rules_to_ledger(ledger, rules)
    log.info(
        "ci_prevention_rules (%s): %d failure event(s) → %d rule(s); ledger %s",
        board_id,
        len(failures),
        len(rules),
        "rewritten" if changed else "unchanged",
    )
    return CiPreventionRulesPassResult(
        session_id=session_id,
        summary=(
            f"{len(failures)} CI_FAILURE event(s) → {len(rules)} prevention "
            f"rule(s); implement ledger {'rewritten' if changed else 'unchanged'}"
        ),
        rules_written=rules,
    )
