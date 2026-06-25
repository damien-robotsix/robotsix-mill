"""CLI entrypoint for the deploy server.

robotsix-deploy serve    — run the deploy-server HTTP API
"""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from .config import DeploySettings
from .main import create_app


def main(argv: list[str] | None = None) -> None:
    """Entrypoint registered as ``robotsix-deploy`` in pyproject.toml."""
    parser = argparse.ArgumentParser(
        prog="robotsix-deploy",
        description="Central deployment & lifecycle server for the robotsix suite.",
    )
    sub = parser.add_subparsers(dest="command")

    serve_parser = sub.add_parser("serve", help="Run the deploy-server HTTP API")
    serve_parser.add_argument(
        "--host",
        default=None,
        help="Override the DEPLOY_HOST / default bind address.",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Override the DEPLOY_PORT / default listen port.",
    )
    serve_parser.add_argument(
        "--log-level",
        default=None,
        choices=["debug", "info", "warning", "error", "critical"],
        help="Override the DEPLOY_LOG_LEVEL / default log level.",
    )

    args = parser.parse_args(argv)

    if args.command != "serve":
        parser.print_help()
        sys.exit(1)

    settings = DeploySettings()
    host = args.host or settings.host
    port = args.port or settings.port
    log_level = (args.log_level or settings.log_level).lower()

    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    log = logging.getLogger("robotsix_deploy")
    log.info("Starting deploy server on %s:%d (log_level=%s)", host, port, log_level)

    app = create_app()
    uvicorn.run(app, host=host, port=port, log_level=log_level)
