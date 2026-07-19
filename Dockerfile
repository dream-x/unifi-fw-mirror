FROM python:3.13-alpine

# curl does the downloading: it speaks SOCKS5, resumes, and retries
RUN apk add --no-cache curl

COPY sync.py /opt/unifi-fw/sync.py
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV UNIFI_FW_CONFIG=/etc/unifi-fw/config.json \
    SYNC_INTERVAL=2592000

VOLUME /srv/firmware

# no args  -> sync on SYNC_INTERVAL
# with args-> pass straight to sync.py, e.g. `docker run … resolve "Flex 2.5G"`
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
