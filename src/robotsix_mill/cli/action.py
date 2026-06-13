from __future__ import annotations

import argparse
import sys

from . import _client
from ..config import Settings


def _action_list(args: argparse.Namespace, settings: Settings) -> int:
    with _client(settings) as c:
        r = c.get(
            "/proposed-actions",
            params={"repo_id": args.repo_id, "status": args.status},
        )
        if not r.is_success:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            print(f"list failed: {detail}", file=sys.stderr)
            return 1
        for a in r.json():
            rationale = (a.get("rationale") or "")[:80]
            print(
                f"{a['id']}\t{a['source']}\t{a['action_type']}\t"
                f"{a['target_ticket_id']}\t{rationale}"
            )
    return 0


def _action_approve(args: argparse.Namespace, settings: Settings) -> int:
    with _client(settings) as c:
        r = c.post(
            f"/proposed-actions/{args.id}/approve",
            params={"repo_id": args.repo_id},
        )
        if r.is_success:
            data = r.json()
            print(f"action {data['id']} approved — now {data['status']}")
            return 0
        else:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            print(f"approve failed: {detail}", file=sys.stderr)
            return 1


def _action_reject(args: argparse.Namespace, settings: Settings) -> int:
    with _client(settings) as c:
        r = c.post(
            f"/proposed-actions/{args.id}/reject",
            params={"repo_id": args.repo_id},
        )
        if r.is_success:
            data = r.json()
            print(f"action {data['id']} rejected — now {data['status']}")
            return 0
        else:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            print(f"reject failed: {detail}", file=sys.stderr)
            return 1
