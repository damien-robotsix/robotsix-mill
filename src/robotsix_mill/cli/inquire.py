from __future__ import annotations

import argparse

from . import _client, _read_body_from_args
from ..config import Settings
from ..core.models import TicketKind


def _inquire(args: argparse.Namespace, settings: Settings) -> int:
    body = _read_body_from_args(args)
    with _client(settings) as c:
        r = c.post(
            "/tickets",
            json={"title": args.title, "description": body, "kind": TicketKind.INQUIRY},
        )
        r.raise_for_status()
        print(r.json()["id"])
    return 0
