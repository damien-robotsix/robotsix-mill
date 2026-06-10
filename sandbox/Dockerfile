FROM python:3.14-slim@sha256:c845af9399020c7e562969a13689e929074a10fd057acd1b1fad06a2fb068e97

# Install the uv Rust binary (pinned to match CI's version).
ARG UV_VERSION=0.8.15
ENV UV_CACHE_DIR=/tmp/.cache/uv

# hadolint ignore=DL3008,DL4006
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
        nodejs=20.19.2+dfsg-1+deb13u2 \
        npm=9.2.0~ds1-3 \
    && curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | sh \
    && rm -rf /var/lib/apt/lists/*

# Playwright + headless Chromium for the browser-level board smoke gate
# (scripts/board_browser_check.py). The runtime egress proxy
# (sandbox/proxy/filter) blocks Playwright's browser-download CDN, so
# Chromium MUST be baked in at build time — `docker build` has full host
# network. `--with-deps` also pulls the apt libraries Chromium needs.
#
# Image-size impact: Chromium + its shared libraries add roughly ~300 MB
# to the sandbox image.
ARG PLAYWRIGHT_VERSION=1.55.0
# hadolint ignore=DL3013
RUN pip install --no-cache-dir "playwright==${PLAYWRIGHT_VERSION}" \
    && playwright install --with-deps chromium
