FROM alpine:3.21@sha256:48b0309ca019d89d40f670aa1bc06e426dc0931948452e8491e3d65087abc07d

# hadolint ignore=DL3018
RUN apk add --no-cache tinyproxy
COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
COPY filter /etc/tinyproxy/filter
# The tinyproxy apk package does not pre-create its log/run dirs; create
# them (idempotent) before handing ownership to the unprivileged user.
RUN mkdir -p /var/log/tinyproxy /var/run/tinyproxy \
    && chown -R nobody:nobody /var/log/tinyproxy /var/run/tinyproxy
USER nobody
EXPOSE 8888
ENTRYPOINT ["tinyproxy", "-d"]
