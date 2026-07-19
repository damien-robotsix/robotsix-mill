"""``robotsix-mill ticket`` subcommand — create, list, show, approve, and manage tickets."""

from __future__ import annotations

import argparse
import mimetypes
import sys
from pathlib import Path

import httpx

from . import _client, _resolve_repo_id, _read_body_from_args
from ..config import Settings


def _upload_screenshot(c: httpx.Client, ticket_id: str, path: str) -> None:
    """Upload one screenshot to *ticket_id*; warn on failure, never raise.

    The ticket already exists at this point, so a failed upload must not
    fail the whole ``ticket new`` command.
    """
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as e:
        print(f"warning: could not read screenshot {path}: {e}", file=sys.stderr)
        return
    media_type = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    try:
        resp = c.post(
            f"/tickets/{ticket_id}/screenshots",
            files={"file": (p.name, data, media_type)},
        )
        if resp.status_code // 100 != 2:
            print(
                f"warning: screenshot upload failed for {path}: "
                f"HTTP {resp.status_code} {resp.text}",
                file=sys.stderr,
            )
    except httpx.HTTPError as e:
        print(f"warning: screenshot upload failed for {path}: {e}", file=sys.stderr)


def _ticket_new(args: argparse.Namespace, settings: Settings) -> int:
    body = _read_body_from_args(args)
    repo_id = _resolve_repo_id(args)
    if repo_id is None:
        return 2
    with _client(settings) as c:
        r = c.post(
            "/tickets",
            json={"title": args.title, "description": body, "repo_id": repo_id},
        )
        r.raise_for_status()
        ticket_id = r.json()["id"]
        for path_ in getattr(args, "screenshot", None) or []:
            _upload_screenshot(c, ticket_id, path_)
        print(ticket_id)
    return 0


def _ticket_list(args: argparse.Namespace, settings: Settings) -> int:
    params: dict[str, str] = {"state": args.state} if args.state else {}
    if args.repo_id:
        params["repo_id"] = args.repo_id
    with _client(settings) as c:
        r = c.get("/tickets", params=params)
        r.raise_for_status()
        for t in r.json():
            print(f"{t['id']}\t{t['state']}\t{t['title']}")
    return 0


def _ticket_show(args: argparse.Namespace, settings: Settings) -> int:
    with _client(settings) as c:
        r = c.get(f"/tickets/{args.id}")
        r.raise_for_status()
        print(r.json())
        h = c.get(f"/tickets/{args.id}/history")
        print("--- history ---")
        for e in h.json():
            print(f"{e['at']}\t{e['state']}\t{e.get('note')}")
    return 0


def _ticket_approve(args: argparse.Namespace, settings: Settings) -> int:
    with _client(settings) as c:
        r = c.post(f"/tickets/{args.id}/approve")
        if r.is_success:
            data = r.json()
            print(f"ticket {data['id']} approved — now in {data['state']}")
            return 0
        else:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            print(f"approve failed: {detail}", file=sys.stderr)
            return 1


def _ticket_resume_blocked(args: argparse.Namespace, settings: Settings) -> int:
    with _client(settings) as c:
        r = c.post(
            f"/tickets/{args.id}/resume-blocked",
            json={"note": getattr(args, "note", "") or ""},
        )
        if r.is_success:
            data = r.json()
            print(f"ticket {data['id']} resumed — now in {data['state']}")
            return 0
        else:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            print(f"resume-blocked failed: {detail}", file=sys.stderr)
            return 1
