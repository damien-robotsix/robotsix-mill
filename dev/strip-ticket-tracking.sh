#!/usr/bin/env bash
# Strip per-ticket diary entries from agent memory files while preserving
# general observations.  Operators run this manually against a data
# directory.  Idempotent — safe to run repeatedly.
#
# Usage:
#   ./dev/strip-ticket-tracking.sh [DATA_DIR]
#
#   DATA_DIR defaults to .mill-data/ from the repo root.
#
# Transformations applied (see ticket spec for details):
#   a. Remove ## Proposals / Done / Ignored / Prior proposals sections.
#   b. Strip lines containing full ticket IDs.
#   c. Strip evidence bullets that are purely backtick-wrapped ticket IDs.
#   d. Strip numeric count claims (digit and word forms).
#   e. Strip "Observed in `<ticket-id>`:" prefixes, keep the observation.
#   f. Remove empty sections (heading only, no body).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"
DATA_DIR="${1:-$REPO/.mill-data}"

if [ ! -d "$DATA_DIR" ]; then
    echo "DATA_DIR '$DATA_DIR' does not exist — nothing to do"
    exit 0
fi

# Count *_memory.md files before invoking Python (fast pre-check).
shopt -s nullglob
MEMORY_FILES=("$DATA_DIR"/*_memory.md)
shopt -u nullglob

if [ ${#MEMORY_FILES[@]} -eq 0 ]; then
    echo "No *_memory.md files found in '$DATA_DIR' — nothing to do"
    exit 0
fi

echo "Found ${#MEMORY_FILES[@]} memory file(s) in '$DATA_DIR'"
echo ""

# ---------------------------------------------------------------------------
# Embedded Python 3 script — does all the heavy lifting.
# ---------------------------------------------------------------------------
exec python3 - "$DATA_DIR" <<'PYEOF'
import re, sys, time
from pathlib import Path

data_dir = Path(sys.argv[1])

# --- ticket ID regex (from service.py line 232) ---------------------------
#  YYYYMMDDTHHMMSSZ-<slug>-<4 hex>
_TICKET_ID_RE = re.compile(r'\b\d{8}T\d{6}Z-[a-z0-9-]+-[a-f0-9]{4}\b')

# --- section headings to drop entirely (case-insensitive) -----------------
_DROP_SECTIONS = {
    'proposals', 'done', 'ignored', 'prior proposals',
}

# --- word numbers for count-claim stripping -------------------------------
_WORD_NUMBERS = (
    r'eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|'
    r'eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|'
    r'eighty|ninety|hundred|thousand'
)
_COUNT_CLAIM_RE = re.compile(
    rf'\b(?:\d+|\b(?:{_WORD_NUMBERS})\b)\s+tickets?\b',
    re.IGNORECASE,
)

# --- "Observed in `<id>`: " prefix ----------------------------------------
_OBSERVED_IN_RE = re.compile(
    r"Observed\s+in\s+`[^`]+`\s*:\s*",
    re.IGNORECASE,
)


def strip_lines(text: str) -> tuple[str, int]:
    """Apply line-level stripping.  Returns (cleaned_text, removed_count)."""
    lines = text.split('\n')
    kept: list[str] = []
    removed = 0

    for line in lines:
        # (e) Strip "Observed in `<id>`:" prefix first — must run before
        #     (b) so the rest of the line (no longer containing a ticket
        #     ID) survives the ID-strip pass.
        line = _OBSERVED_IN_RE.sub('', line)

        # (b) Strip lines containing a full ticket ID.
        if _TICKET_ID_RE.search(line):
            removed += 1
            continue

        # (c) Strip evidence bullets that are purely backtick-wrapped IDs:
        #     `- `TKT-001`` with optional trailing whitespace.
        stripped = line.rstrip()
        if re.match(r'^\s*[-*]\s+`[^`]+`\s*$', stripped):
            removed += 1
            continue

        # (d) Strip numeric count claims.
        if _COUNT_CLAIM_RE.search(line):
            removed += 1
            continue

        kept.append(line)

    return '\n'.join(kept), removed


def strip_sections(text: str) -> tuple[str, int]:
    """Remove whole ## sections whose heading matches the drop set (a),
    then remove any section whose body is now empty (f).

    Returns (cleaned_text, sections_removed)."""
    # Split on "\n## " at line start (heading boundaries).
    # If the text starts with "## " the first chunk after a plain
    # split would land in the preamble and escape the section loop.
    # Prepend a sentinel newline so the first heading is always in
    # sections[0] and preamble is guaranteed empty-or-prose.
    if not text.strip():
        return text, 0

    if text.startswith('## '):
        parts = re.split(r'\n(?=## )', '\n' + text)
    else:
        parts = re.split(r'\n(?=## )', text)
    preamble = parts[0].lstrip('\n')  # drop sentinel
    sections = parts[1:]

    kept_sections: list[str] = []
    removed = 0

    for section in sections:
        # Extract heading text (the line after "## " up to newline).
        heading_match = re.match(r'##\s+(.+)', section)
        if not heading_match:
            kept_sections.append(section)
            continue

        heading = heading_match.group(1).strip().lower()

        # (a) Drop sections whose heading matches the drop set.
        if heading in _DROP_SECTIONS:
            removed += 1
            continue

        # (f) Remove empty sections: heading line only, no body content.
        body = section[heading_match.end():].strip()
        if not body:
            removed += 1
            continue

        kept_sections.append(section)

    result = preamble
    if kept_sections:
        # Ensure sections are separated by a blank line from the preamble
        # if the preamble is non-empty.
        if preamble.rstrip():
            result = preamble.rstrip() + '\n\n' + '\n'.join(kept_sections)
        else:
            result = '\n'.join(kept_sections)

    # Normalise: no trailing whitespace, exactly one trailing newline.
    result = result.rstrip() + '\n'
    return result, removed


def process_file(memory_path: Path) -> str:
    """Process one memory file.  Returns a one-line summary string."""
    name = memory_path.name

    # Read as text — skip on encoding errors.
    try:
        original = memory_path.read_text(encoding='utf-8')
    except (UnicodeDecodeError, OSError) as exc:
        return f"{name}: SKIPPED (cannot read as UTF-8: {exc})"

    if not original.strip():
        return f"{name}: SKIPPED (empty file)"

    # Phase 1: line-level stripping.
    after_lines, line_removed = strip_lines(original)

    # Phase 2: section-level stripping.
    after_sections, sec_removed = strip_sections(after_lines)

    total_removed = line_removed + sec_removed

    if total_removed == 0:
        return f"{name}: SKIPPED (no per-ticket content found)"

    # Create timestamped backup.
    ts = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    backup_path = memory_path.with_suffix(f'.pre-strip-{ts}.bak')
    backup_path.write_text(original, encoding='utf-8')

    # Write cleaned content.
    memory_path.write_text(after_sections, encoding='utf-8')

    return (
        f"{name}: stripped {total_removed} item(s) "
        f"({line_removed} line-level, {sec_removed} section-level) "
        f"— backup: {backup_path.name}"
    )


def main() -> None:
    memory_files = sorted(data_dir.glob('*_memory.md'))
    if not memory_files:
        print("No *_memory.md files found — nothing to do")
        return

    processed = 0
    skipped = 0
    for mf in memory_files:
        summary = process_file(mf)
        print(summary)
        if 'SKIPPED' in summary:
            skipped += 1
        else:
            processed += 1

    print(f"\nDone: {processed} file(s) modified, {skipped} file(s) skipped")

if __name__ == '__main__':
    main()
PYEOF
