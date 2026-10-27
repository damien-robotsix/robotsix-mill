FROM alpine:3.21@sha256:48b0309ca019d89d40f670aa1bc06e426dc0931948452e8491e3d65087abc07d

# Upgrade OpenSSL past the pinned base's 3.3.7-r0 (CVE-2026-45447,
# CVE-2026-84782 — fixed in 3.3.7-r2) so the Trivy HIGH gate passes.
# hadolint ignore=DL3018
RUN apk upgrade --no-cache libcrypto3 libssl3 \
    && apk add --no-cache tinyproxy
# COPY paths are relative to the repository root (context: .) — the reusable
# docker-release.yml workflow always builds with context: . and does not
# expose a `context` input. Keep these paths anchored at the repo root so the
# Dockerfile works regardless of whether the caller sets context to .
# or sandbox/proxy.
COPY sandbox/proxy/tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
COPY sandbox/proxy/filter /etc/tinyproxy/filter
# The tinyproxy apk package does not pre-create its log/run dirs; create
# them (idempotent) before handing ownership to the unprivileged user.
RUN mkdir -p /var/log/tinyproxy /var/run/tinyproxy \
    && chown -R nobody:nobody /var/log/tinyproxy /var/run/tinyproxy
USER nobody
EXPOSE 8888
ENTRYPOINT ["tinyproxy", "-d"]
