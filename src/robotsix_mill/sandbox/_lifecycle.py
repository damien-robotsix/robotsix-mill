"""Container lifecycle management."""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

from ..config import Settings


def run(
    command: str,
    *,
    repo_dir: Path,
    settings: Settings,
    install_project: bool = True,
    sandbox_image: str | None = None,
) -> tuple[int, str]:
    """Execute ``command`` against ``repo_dir`` in a disposable
    container. Returns ``(exit_code, combined_output)``. Raises
    :class:`SandboxError` on isolation-infrastructure failure.

    By default the repo's own dependencies are installed before
    *command* runs so that the workspace clone — not the image's frozen
    site-packages — is the imported tree.  Callers that must skip the
    install (e.g. ad-hoc commands in an environment with no egress
    proxy) can pass *install_project=False*.  See
    ``_maybe_install_prefix`` for how the install is made
    read-only-safe.
    """
    # Lazy imports to avoid circular dependency with __init__.py.
    from robotsix_mill.sandbox import (
        _CACHE_TARGET,
        SandboxError,
        _build_extra_packages_prefix,
        _cache_mount,
        _maybe_install_prefix,
        _repo_mount,
        _sandbox_slot,
        _truncate,
        ensure_sandbox_network,
        load_extra_sandbox_packages,
        log,
    )

    # Callers (e.g. the merge stage) may pass a str. We also resolve to
    # an absolute path because Docker's `-w` rejects relative arguments
    # (see _repo_mount for the same reason).
    repo_dir = Path(repo_dir).resolve()
    # Deploy mode only (DOCKER_HOST points at central-deploy's socket-proxy):
    # central-deploy ignores the dev stack's `networks:` block, so the
    # internal egress network + proxy attachment must be established at
    # runtime. Runs before EVERY spawn (idempotent, two fast docker CLI
    # calls): a deploy can recreate the sandbox-proxy sibling at any time,
    # detaching it from the network — a once-per-process guard left all
    # subsequent sandboxes without egress until the mill itself restarted
    # (2026-07-05 incident: every test suite failed with pytest missing).
    # The dev stack (no DOCKER_HOST) skips it entirely and is unchanged.
    if os.environ.get("DOCKER_HOST") and settings.sandbox_proxy_url:
        ensure_sandbox_network(settings)
    # Load extra sandbox packages declared in the repo's config.
    extra_packages = load_extra_sandbox_packages(repo_dir)
    # Resolved early so pip extras can be installed ONCE into the shared,
    # disk-backed cache instead of per call into the container's tmpfs.
    cache_argv = _cache_mount(settings)
    extra_prefix, needs_write_access = _build_extra_packages_prefix(
        extra_packages, cache_target=_CACHE_TARGET if cache_argv else None
    )
    if extra_prefix:
        log.info("Installing extra sandbox packages: %s", extra_packages)
        if needs_write_access:
            log.info(
                "Dropping --read-only and adding tmpfs mounts for apt "
                "package installation"
            )
    name = f"mill-sbx-{uuid.uuid4().hex[:12]}"
    argv = [
        "docker",
        "run",
        "--rm",
        # Sandboxes must not inherit the image's HEALTHCHECK. sandbox_image is
        # routinely the mill image itself (that is what the deployed mill pins),
        # and mill's HEALTHCHECK curls its own API on localhost:8077 every 30s.
        # No mill server runs inside a sandbox, so it is ConnectionRefused
        # forever: Docker spawns a fresh CPython in every sandbox every 30s
        # purely to fail, and `docker ps` shows every sandbox as (unhealthy) —
        # actively misleading during triage, since the sandboxes are fine.
        # Observed 2026-08-25: five live sandboxes, FailingStreak up to 49.
        "--no-healthcheck",
        "--name",
        name,
        "--network",
        settings.sandbox_network if settings.sandbox_proxy_url else "none",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--pids-limit",
        str(settings.sandbox_pids_limit),
        "--memory",
        settings.sandbox_memory,
        "--tmpfs",
        # Mount exec (Docker's default tmpfs options include noexec): pip
        # --user console scripts land under $HOME/.local/bin = /tmp/.local/bin
        # and must be executable. Keep nosuid/nodev hardening.
        #
        # size= matters: an unsized Docker tmpfs defaults to half of HOST RAM,
        # so /tmp could grow until it hit the container's memory cgroup and the
        # kernel OOM-killed the test process — a confusing "Killed" with no
        # explanation. Sized, an overflowing sandbox fails loudly with ENOSPC.
        f"/tmp:exec,rw,nosuid,nodev,size={settings.sandbox_tmpfs_size}",  # nosec B108 — /tmp here is a Docker tmpfs INSIDE the sandbox, not the host's
        "-e",
        "HOME=/tmp",  # nosec B108
        "-e",
        "GIT_TERMINAL_PROMPT=0",
        *_repo_mount(repo_dir, settings),
        "-w",
        str(repo_dir),
    ]
    # Bound CPU the way memory and PIDs already are. Without this the
    # concurrency cap bounds the sandbox COUNT while host load stays
    # unbounded — N sandboxes each take whatever their test command's
    # parallelism allows, which is what kept the safe cap so low.
    if settings.sandbox_cpus > 0:
        # Docker wants a plain decimal; repr() on a float can emit
        # scientific notation for very small values, which it rejects.
        argv += ["--cpus", f"{settings.sandbox_cpus:.3f}".rstrip("0").rstrip(".")]
    # Keep the package caches OUT of the RAM-backed /tmp (see _cache_mount).
    # XDG_CACHE_HOME covers uv's default location and most other tools;
    # UV_CACHE_DIR/PIP_CACHE_DIR are set explicitly so neither depends on it.
    if cache_argv:
        argv += cache_argv
        argv += [
            "-e",
            f"XDG_CACHE_HOME={_CACHE_TARGET}",
            "-e",
            f"UV_CACHE_DIR={_CACHE_TARGET}/uv",
            "-e",
            f"PIP_CACHE_DIR={_CACHE_TARGET}/pip",
        ]
    if needs_write_access:
        # apt must write to the root filesystem — drop --read-only and
        # add tmpfs mounts so apt state dirs don't dirty the overlay.
        argv += [
            "--tmpfs",
            "/var/cache/apt",
            "--tmpfs",
            "/var/lib/apt/lists",
            "--tmpfs",
            "/var/lib/dpkg",
        ]
    elif settings.sandbox_readonly:
        argv.append("--read-only")
    # When the mounted repo has a src/ layout, put its source first on
    # PYTHONPATH so the command runs against the MOUNTED code — not a
    # stale copy of the package baked into the sandbox image's
    # site-packages. Without this the in-sandbox test gate silently
    # validates the image's old code instead of the ticket's edits.
    if (repo_dir / "src").is_dir():
        argv += ["-e", "PYTHONPATH=src"]
    # Route HTTP/HTTPS through the egress proxy so only allowlisted
    # domains (PyPI, GitHub) are reachable from the sandbox.
    if settings.sandbox_proxy_url:
        proxy = settings.sandbox_proxy_url
        # Loopback must bypass the proxy: a repo's own test suite often spins
        # up a localhost HTTP server and connects to it (e.g. auto-mail's
        # tests/test_server.py). Without no_proxy those connections get routed
        # to the egress proxy and fail (the proxy filters non-allowlisted
        # hosts, and the suite's network guard flags the real connection).
        no_proxy = "localhost,127.0.0.1,::1"
        argv += [
            "-e",
            f"HTTP_PROXY={proxy}",
            "-e",
            f"HTTPS_PROXY={proxy}",
            "-e",
            f"http_proxy={proxy}",
            "-e",
            f"https_proxy={proxy}",
            "-e",
            f"NO_PROXY={no_proxy}",
            "-e",
            f"no_proxy={no_proxy}",
        ]
    # Optionally prefix a dependency install so the gate runs against the
    # repo's DECLARED deps, not just the image's frozen ones.
    # Extra packages are installed FIRST so they are available when the
    # project build runs (and when the user command executes).
    effective_command = extra_prefix + command
    if install_project:
        effective_command = _maybe_install_prefix(effective_command, repo_dir, settings)

    # Put the pip ``--user`` scripts dir on PATH so console-script entry
    # points (e.g. yamllint installed via extra_sandbox_packages) resolve.
    # pip installs them under ``$HOME/.local/bin`` = ``/tmp/.local/bin``
    # (HOME is fixed to /tmp above) — a dir NOT on the image's PATH, so
    # without this a gate calling such a script dies with rc=127. The
    # export must live inside the ``sh -lc`` string (docker ``-e`` does
    # no shell expansion) and be the FIRST statement so it is in effect
    # for the extra-package install, the project install, and the user
    # command alike.
    effective_command = (
        'export PATH="$HOME/.local/bin:/tmp/.local/bin:$PATH"; ' + effective_command
    )

    # Override the image ENTRYPOINT: images like robotsix/mill have one
    # (it starts the server) which would otherwise swallow our command.
    # Per-repo override (sandbox_image) wins; None falls back to the
    # fleet-wide settings.sandbox_image so existing callers are unchanged.
    image = sandbox_image or settings.sandbox_image
    argv += ["--entrypoint", "sh", image, "-lc", effective_command]

    # Per-op timeout: use the dedicated sandbox_op_timeout when set,
    # falling back to the legacy command_timeout (1800s).  A single
    # hung docker exec must fast-fail in ~5 min (default 300s) instead
    # of silently draining the stage budget for 30 min.
    op_timeout = (
        settings.sandbox_op_timeout
        if settings.sandbox_op_timeout > 0
        else settings.command_timeout
    )
    max_attempts = 3
    # The slot is held across the retries on purpose: a retry re-spawns the
    # same container name, so releasing between attempts would let the live
    # count exceed the cap. It is taken as late as possible — argv building
    # and the extra-package probe above touch no containers.
    with _sandbox_slot(settings):
        for attempt in range(1, max_attempts + 1):
            try:
                r = subprocess.run(
                    argv,
                    capture_output=True,
                    text=False,
                    timeout=op_timeout,
                )
            except FileNotFoundError as e:
                raise SandboxError("docker CLI not found in the mill image") from e
            except subprocess.TimeoutExpired:
                # the `docker run` client was killed; force-remove the container
                subprocess.run(
                    ["docker", "rm", "-f", name], capture_output=True, text=False
                )
                return 124, f"command timed out after {op_timeout}s"

            stdout = r.stdout.decode("utf-8", errors="replace") if r.stdout else ""
            stderr = r.stderr.decode("utf-8", errors="replace") if r.stderr else ""
            # 125 == docker daemon/usage error (not the command's own exit code)
            if r.returncode == 125:
                eof_msg = stderr.strip()
                if "unexpected EOF" in eof_msg and attempt < max_attempts:
                    log.warning(
                        "sandbox spawn attempt %d/%d: docker wait stream EOF "
                        "(socket-proxy timeout likely); retrying...",
                        attempt,
                        max_attempts,
                    )
                    # Clean up any container that may have leaked
                    subprocess.run(
                        ["docker", "rm", "-f", name],
                        capture_output=True,
                        text=False,
                    )
                    continue
                raise SandboxError(f"docker run failed: {eof_msg[:300]}")
            return r.returncode, _truncate(stdout + stderr)
    # Should not be reachable — the last attempt either returns or raises
    # above (non-125 → return; 125 non-EOF → raise; 125 EOF on last
    # attempt → raise).  Included as a safety net.
    raise SandboxError("docker run failed: unexpected EOF after all retries")
