#!/usr/bin/env python3
"""Resolve a Docker image:tag to a sha256 digest.

Usage:
    python3 scripts/resolve_docker_digest.py python:3.14-slim
    python3 scripts/resolve_docker_digest.py python:3.14-slim --platform linux/amd64
    python3 scripts/resolve_docker_digest.py ghcr.io/owner/image:tag

Uses the Docker Hub REST API for library images
(https://hub.docker.com/v2/repositories/library/{image}/tags/{tag}),
and the OCI registry token+manifest flow for other registries.
No authentication required for public images.
"""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from urllib.error import HTTPError, URLError


def _hub_api_digest(image: str, tag: str, platform: str) -> str:
    """Resolve a digest for a Docker Hub library image via the Hub REST API."""
    url = f"https://hub.docker.com/v2/repositories/library/{image}/tags/{tag}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())

    images = data.get("images", [])
    for entry in images:
        if (
            entry.get("architecture") == platform.split("/")[-1]
        ):  # "linux/amd64" → "amd64"
            digest = entry.get("digest", "")
            if digest:
                return digest

    # Fallback: return the first digest found
    for entry in images:
        digest = entry.get("digest", "")
        if digest:
            return digest

    raise SystemExit(
        f"Could not find a digest for {image}:{tag} (platform={platform}). "
        f"Available images: {json.dumps(images, indent=2)}"
    )


def _registry_token(repo: str, registry: str, service: str) -> str:
    """Get a Bearer token for a registry repository."""
    url = f"https://{registry}/token?service={service}&scope=repository:{repo}:pull"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    token = data.get("token", "")
    if not token:
        raise SystemExit(f"No token returned from {url}: {data}")
    return token


def _registry_digest(
    repo: str, ref: str, token_host: str, manifest_host: str, service: str
) -> str:
    """Resolve a digest via the OCI registry manifest API (Docker-Content-Digest header)."""
    token = _registry_token(repo, token_host, service)

    # Accept both OCI and Docker v2 manifest formats.
    # The registry returns the digest in the Docker-Content-Digest response header.
    url = f"https://{manifest_host}/v2/{repo}/manifests/{ref}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": (
                "application/vnd.oci.image.manifest.v1+json, "
                "application/vnd.docker.distribution.manifest.v2+json, "
                "application/vnd.oci.image.index.v1+json, "
                "application/vnd.docker.distribution.manifest.list.v2+json"
            ),
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        digest = resp.headers.get("Docker-Content-Digest", "")
        if not digest:
            # Compute SHA256 of the manifest body as a fallback
            digest = "sha256:" + hashlib.sha256(resp.read()).hexdigest()
        return digest


def resolve_digest(image_ref: str, platform: str = "linux/amd64") -> str:
    """Resolve a Docker image reference to a sha256 digest.

    Supported registries:
    - Docker Hub: library images via the Hub API
    - Docker Hub: non-library images via the registry v2 API
    - ghcr.io: via the registry v2 API
    """
    # Split image:tag
    if ":" not in image_ref:
        raise SystemExit(
            f"Image reference must include a tag: {image_ref!r}. "
            f"Usage: resolve_docker_digest.py <image>:<tag>"
        )

    # Parse the image reference into registry, repo, image, tag
    parts = image_ref.split(":")
    tag = parts[-1]
    name_part = ":".join(parts[:-1])  # handle cases like "ghcr.io/owner/image:tag"

    # Determine registry
    if "/" not in name_part:
        # Bare image name — Docker Hub library image
        image = name_part
        return _hub_api_digest(image, tag, platform)

    # Full path: registry/namespace/image or namespace/image
    slash_parts = name_part.split("/")

    if len(slash_parts) == 1:
        # "library/image" → Docker Hub library image
        image = slash_parts[0]
        return _hub_api_digest(image, tag, platform)

    if len(slash_parts) >= 2:
        first = slash_parts[0]
        if "." in first:  # registry hostname (docker.io, ghcr.io, etc.)
            registry = first
            repo = "/".join(slash_parts[1:])
        elif first == "library":
            # Explicit "library/image" → Docker Hub library image
            return _hub_api_digest(slash_parts[1], tag, platform)
        else:
            # Docker Hub user/org repo: "namespace/image"
            registry = "registry-1.docker.io"
            repo = "/".join(slash_parts)

        if registry == "ghcr.io":
            return _registry_digest(repo, tag, "ghcr.io", "ghcr.io", "ghcr.io")
        # Docker Hub non-library images: token goes to auth.docker.io,
        # manifest goes to registry-1.docker.io.
        if registry in ("docker.io", "registry-1.docker.io"):
            return _registry_digest(
                repo,
                tag,
                "auth.docker.io",
                "registry-1.docker.io",
                "registry.docker.io",
            )
        # Other registries (quay.io, etc.): use the registry host for
        # both token and manifest.
        return _registry_digest(repo, tag, registry, registry, registry)

    raise SystemExit(f"Could not parse image reference: {image_ref!r}")


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "Usage: python3 scripts/resolve_docker_digest.py <image>:<tag> "
            "[--platform linux/amd64]",
            file=sys.stderr,
        )
        sys.exit(1)

    image_ref = sys.argv[1]
    platform = "linux/amd64"

    # Parse --platform flag
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--platform" and i + 1 < len(args):
            platform = args[i + 1]
            i += 2
        else:
            print(f"Unknown argument: {args[i]}", file=sys.stderr)
            sys.exit(1)

    try:
        digest = resolve_digest(image_ref, platform)
    except (HTTPError, URLError) as exc:
        raise SystemExit(f"Failed to resolve {image_ref}: {exc}") from exc

    print(digest)


if __name__ == "__main__":
    main()
