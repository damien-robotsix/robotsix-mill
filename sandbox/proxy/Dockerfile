FROM alpine:3.21@sha256:a8560b36e8b821534b54f2f2629c1c9115f1c54f9f5e2f8e99a5e79b6e5c8e4f

# hadolint ignore=DL3018
RUN apk add --no-cache tinyproxy
COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
RUN chown -R nobody:nobody /var/log/tinyproxy /var/run/tinyproxy
USER nobody
EXPOSE 8888
ENTRYPOINT ["tinyproxy", "-d"]
