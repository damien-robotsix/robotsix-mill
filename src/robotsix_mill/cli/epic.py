"""CLI subcommand for listing epic children."""

from __future__ import annotations

import argparse

from ..config import Settings
from . import _client, _read_body_from_args, _resolve_repo_id


def _epic_new(args: argparse.Namespace, settings: Settings) -> int:
    body = _read_body_from_args(args)
    repo_id = _resolve_repo_id(args)
    if repo_id is None:
        return 2
    with _client(settings) as c:
        r = c.post(
            "/epics",
            json={"title": args.title, "description": body, "repo_id": repo_id},
        )
        r.raise_for_status()
        print(r.json()["id"])
    return 0
