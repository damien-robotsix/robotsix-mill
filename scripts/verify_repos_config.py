#!/usr/bin/env python3
"""Verify that config/repos.yaml is valid and contains real credentials.

Usage:
    python scripts/verify_repos_config.py

Exit codes:
    0 — config is valid and all credentials are real (not placeholders).
    1 — config is missing, invalid, or contains placeholder/TODO values.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the repo root is on sys.path so absolute 'import robotsix_mill' works.
_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

# The robotsix_mill package lives under src/.
_src = _repo_root / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))


def main() -> int:
    from robotsix_mill.config import get_repo_config, load_repos_config

    # ---------- 1. Load the registry ----------
    try:
        registry = load_repos_config()
    except Exception as exc:
        print(f"FAIL: could not load repos config: {exc}", file=sys.stderr)
        return 1

    repo_count = len(registry.repos)
    if repo_count != 1:
        print(
            f"FAIL: expected exactly 1 repo entry, found {repo_count}",
            file=sys.stderr,
        )
        return 1

    # ---------- 2. Look up the robotsix-mill entry ----------
    try:
        repo = get_repo_config("robotsix-mill")
    except Exception as exc:
        print(f"FAIL: get_repo_config('robotsix-mill') raised: {exc}", file=sys.stderr)
        return 1

    # ---------- 3. Field-level assertions ----------
    errors: list[str] = []

    if not repo.repo_id:
        errors.append("repo_id is empty")
    if repo.repo_id != "robotsix-mill":
        errors.append(f"repo_id is '{repo.repo_id}', expected 'robotsix-mill'")

    if not repo.langfuse_project_name:
        errors.append("langfuse_project_name is empty")

    pk = repo.langfuse_public_key
    if not pk:
        errors.append("langfuse_public_key is empty")
    elif not pk.startswith("pk-lf-"):
        errors.append(
            f"langfuse_public_key ('{pk[:20]}...') does not start with 'pk-lf-'"
        )
    elif len(pk) < 20:
        errors.append(
            f"langfuse_public_key is too short ({len(pk)} chars, expected ≥ 20)"
        )

    sk = repo.langfuse_secret_key
    if not sk:
        errors.append("langfuse_secret_key is empty")
    elif not sk.startswith("sk-lf-"):
        errors.append(
            f"langfuse_secret_key ('{sk[:20]}...') does not start with 'sk-lf-'"
        )
    elif len(sk) < 20:
        errors.append(
            f"langfuse_secret_key is too short ({len(sk)} chars, expected ≥ 20)"
        )

    if errors:
        for err in errors:
            print(f"FAIL: {err}", file=sys.stderr)
        print(
            "\nHint: create a Langfuse project named 'robotsix-mill' at\n"
            "https://cloud.langfuse.com, then replace the placeholder keys in\n"
            "config/repos.yaml with the real pk-lf-... / sk-lf-... values.",
            file=sys.stderr,
        )
        return 1

    # ---------- 4. Success ----------
    print("repos config OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
