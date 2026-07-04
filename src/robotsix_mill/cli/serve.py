from __future__ import annotations

import argparse
import sys

from ..config import Settings


def _serve(args: argparse.Namespace, settings: Settings) -> int:
    # Raise nofile soft cap: docker-compose's ulimits only set the
    # hard cap, and PAM (via runuser in the container entrypoint)
    # clamps the soft back to 1024. Workers cascade-crash with
    # OSError: [Errno 24] once they exhaust it across parallel
    # git/trivy/agent subprocesses.
    import resource

    try:
        _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = max(65536, hard) if hard != resource.RLIM_INFINITY else 65536
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except ValueError, OSError:
        pass

    import uvicorn

    from ..runtime.api import create_app
    from ..config import get_repos_config
    from ..config import ConfigError

    if args.repo_id:
        # Single-repo override for tests/dev.
        try:
            repos = get_repos_config()
        except ConfigError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        if args.repo_id not in repos.repos:
            known = sorted(repos.repos.keys())
            print(
                f"Error: Unknown repo '{args.repo_id}'. Known repos: {known}",
                file=sys.stderr,
            )
            return 2
        single_repo_id: str | None = args.repo_id
    else:
        # Multi-repo mode: load all repos from config/repos.yaml.
        try:
            repos = get_repos_config()
        except ConfigError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        single_repo_id = None

    uvicorn.run(
        create_app(repos, settings, single_repo_id=single_repo_id),
        host=settings.api_host,
        port=settings.api_port,
    )
    return 0


def _repos_list(args: argparse.Namespace, settings: Settings) -> int:
    from ..config import get_repos_config
    from ..config import ConfigError

    try:
        repos = get_repos_config()
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(f"{'REPO_ID':30s} {'SOURCE'}")
    for rc in repos.repos.values():
        print(f"{rc.repo_id:30s} {rc.source}")
    return 0
