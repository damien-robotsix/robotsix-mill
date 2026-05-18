"""``robotsix-mill`` CLI — a thin HTTP client over the service.

    robotsix-mill serve                       # run the API + worker
    robotsix-mill ticket new --title T [--description-file F | -]
    robotsix-mill ticket list [--state S]
    robotsix-mill ticket show <id>
    robotsix-mill ticket approve <id>

The same API backs a future web frontend.
"""

from __future__ import annotations

import argparse
import sys

import httpx

from .config import Settings
from .core.states import State


def _client(settings: Settings) -> httpx.Client:
    return httpx.Client(base_url=settings.api_url, timeout=30.0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="robotsix-mill")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="run the API + event-driven worker")

    p_ticket = sub.add_parser("ticket", help="ticket operations")
    tsub = p_ticket.add_subparsers(dest="tcmd", required=True)

    p_new = tsub.add_parser("new", help="emit a ticket (worker picks it up)")
    p_new.add_argument("--title", required=True)
    p_new.add_argument(
        "--description-file", help="file with the body; '-' reads stdin"
    )

    p_list = tsub.add_parser("list", help="list tickets")
    p_list.add_argument("--state", choices=[s.value for s in State])

    p_show = tsub.add_parser("show", help="show one ticket + history")
    p_show.add_argument("id")

    p_approve = tsub.add_parser(
        "approve", help="approve a ticket in awaiting_approval state"
    )
    p_approve.add_argument("id")

    args = parser.parse_args(argv)
    settings = Settings()

    if args.cmd == "serve":
        import uvicorn

        from .runtime.api import create_app

        uvicorn.run(
            create_app(settings), host=settings.api_host, port=settings.api_port
        )
        return 0

    with _client(settings) as c:
        if args.tcmd == "new":
            body = ""
            if args.description_file == "-":
                body = sys.stdin.read()
            elif args.description_file:
                with open(args.description_file, encoding="utf-8") as f:
                    body = f.read()
            r = c.post(
                "/tickets", json={"title": args.title, "description": body}
            )
            r.raise_for_status()
            print(r.json()["id"])
            return 0
        if args.tcmd == "list":
            params = {"state": args.state} if args.state else {}
            r = c.get("/tickets", params=params)
            r.raise_for_status()
            for t in r.json():
                print(f"{t['id']}\t{t['state']}\t{t['title']}")
            return 0
        if args.tcmd == "show":
            r = c.get(f"/tickets/{args.id}")
            r.raise_for_status()
            print(r.json())
            h = c.get(f"/tickets/{args.id}/history")
            print("--- history ---")
            for e in h.json():
                print(f"{e['at']}\t{e['state']}\t{e.get('note')}")
            return 0
        if args.tcmd == "approve":
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

    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
