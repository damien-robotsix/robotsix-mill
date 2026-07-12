FROM python:3.14-slim@sha256:c845af9399020c7e562969a13689e929074a10fd057acd1b1fad06a2fb068e97

# Install the uv Rust binary (pinned to match CI's version).
ARG UV_VERSION=0.8.15
ENV UV_CACHE_DIR=/tmp/.cache/uv

# UV_INSTALL_DIR pins uv into a world-readable system path ON $PATH.
# `docker build` runs as root, so the installer's default target is
# /root/.local/bin — but the sandbox is launched as a NON-root user
# (`--user $UID:$GID`, HOME=/tmp), and /root is mode 0700, so a binary
# left there is both off-PATH and unreadable at runtime ("uv: not
# found", which silently breaks `uv lock` / `uv sync` for every repo).
# Installing to /usr/local/bin (same trick as Chromium below) makes uv
# actually usable by the runtime user.
ENV UV_INSTALL_DIR=/usr/local/bin

# hadolint ignore=DL3008,DL4006
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
        nodejs=20.19.2+dfsg-1+deb13u2 \
        npm=9.2.0~ds1-3 \
    && curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | sh \
    && chmod a+rx /usr/local/bin/uv \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI (`gh`) for driving contribution workflows (push -> PR -> merge)
# from inside the sandbox. `gh` is not in Debian's default apt repos, so add
# the official GitHub CLI apt source first. `docker build` has full host
# network (the runtime egress proxy does not apply at build time, as with the
# Playwright/Chromium layer below), so fetching the keyring here is fine.
# `curl` is already installed in the first RUN layer above.
# hadolint ignore=DL3008,DL4006
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Playwright + headless Chromium for the browser-level board smoke gate
# (scripts/board_browser_check.py). The runtime egress proxy
# (sandbox/proxy/filter) blocks Playwright's browser-download CDN, so
# Chromium MUST be baked in at build time — `docker build` has full host
# network. `--with-deps` also pulls the apt libraries Chromium needs.
#
# Image-size impact: Chromium + its shared libraries add roughly ~300 MB
# to the sandbox image. That is under the ~500 MB threshold, so the
# browser is baked into this single dedicated image — no opt-in variant
# is needed (see docs/config/configuration.md, sandbox.image entry).
#
# PLAYWRIGHT_BROWSERS_PATH pins the browser cache to a fixed, world-
# readable system path OUTSIDE any user $HOME. `playwright install` runs
# as root here and writes Chromium into /ms-playwright; the image ENV
# survives `docker run`, and sandbox.py launches the container with
# `--user $UID:$GID` + `HOME=/tmp` WITHOUT overriding
# PLAYWRIGHT_BROWSERS_PATH, so the non-root runtime user reads the same
# baked browser. `chmod -R a+rX` makes the install tree readable and
# traversable by that non-root user (the egress proxy blocks the
# Playwright download CDN, so no runtime download is possible).
ARG PLAYWRIGHT_VERSION=1.55.0
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
# hadolint ignore=DL3013
RUN pip install --no-cache-dir "playwright==${PLAYWRIGHT_VERSION}" \
    && playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright

# Test-gate tooling for every board whose test_command is `pytest …`.
# The per-run `pip install --user .` only provides each repo's RUNTIME
# deps (dev extras / PEP 735 dependency-groups never reach the sandbox),
# so the gate binaries must be baked into the image. Keep this list to
# the universal gate surface; repo-specific needs go through
# extra_sandbox_packages in each repo's .robotsix-mill/config.yaml.
# hadolint ignore=DL3013
RUN pip install --no-cache-dir pytest pytest-asyncio pytest-cov
