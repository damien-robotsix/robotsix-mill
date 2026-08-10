#!/usr/bin/env python3
"""Pre-commit hook: fail on expired CVE suppressions in .trivyignore.

Parses .trivyignore for lines of the form::

    CVE-YYYY-NNNNN  # expires: YYYY-MM-DD

and exits non-zero when any expiry date is today or in the past.
"""

import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path

_EXPIRY_RE = re.compile(r"^(CVE-\d{4}-\d{4,})\s+#\s*expires:\s*(\d{4}-\d{2}-\d{2})\s*$")


def main() -> int:
    trivyignore = Path(__file__).resolve().parent.parent / ".trivyignore"

    try:
        lines = trivyignore.read_text().splitlines()
    except FileNotFoundError:
        print(f"error: {trivyignore} not found", file=sys.stderr)
        return 1

    today = datetime.now(tz=UTC).date()
    expired: list[tuple[str, date]] = []

    for line in lines:
        m = _EXPIRY_RE.match(line.strip())
        if m is None:
            continue
        cve_id = m.group(1)
        expiry_str = m.group(2)
        try:
            expiry = date.fromisoformat(expiry_str)
        except ValueError:
            print(
                f"error: invalid expiry date '{expiry_str}' for {cve_id}",
                file=sys.stderr,
            )
            return 1
        if expiry <= today:
            expired.append((cve_id, expiry))

    if expired:
        print(
            "Expired CVE suppressions found in .trivyignore.\n"
            "Either remove the suppression (if the vulnerability is fixed) "
            "or extend the expiry date:\n",
            file=sys.stderr,
        )
        for cve_id, expiry in expired:
            print(f"  {cve_id}  # expired: {expiry.isoformat()}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
