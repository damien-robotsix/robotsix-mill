"""web_fetch tool: GET a URL via the dedicated network-enabled fetch
sandbox (see sandbox.fetch — no repo/data mount, fixed curl).
"""

from __future__ import annotations

from ..config import Settings


def make_web_fetch(settings: Settings):
    def web_fetch(url: str) -> str:
        """Fetch an http(s) URL and return its body as text (size
        capped). Use for official docs, source files, package metadata.
        Runs in an isolated, no-local-access network container."""
        from .. import sandbox

        try:
            rc, body = sandbox.fetch(url, settings=settings)
        except sandbox.SandboxError as e:
            return f"fetch error: {e}"
        return body if rc == 0 else f"fetch failed: {body}"

    return web_fetch
