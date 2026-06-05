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
