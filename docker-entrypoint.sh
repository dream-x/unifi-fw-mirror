#!/bin/sh
# Three modes:
#   args given        -> run sync.py with them and exit (resolve, sync --check, …)
#   SYNC_INTERVAL=0   -> sync once and exit, for host cron or a one-shot job
#   otherwise         -> sync now, then every SYNC_INTERVAL seconds
set -u

SYNC="/opt/unifi-fw/sync.py"

if [ "$#" -gt 0 ]; then
    exec python3 "$SYNC" "$@"
fi

if [ "${SYNC_INTERVAL}" = "0" ]; then
    exec python3 "$SYNC" sync
fi

while :; do
    # a failed sync must not kill the loop -- the next cycle may well succeed,
    # and the mirror still serves whatever it already holds
    python3 "$SYNC" sync || echo "sync failed; retrying in ${SYNC_INTERVAL}s" >&2
    echo "next sync in ${SYNC_INTERVAL}s"
    sleep "${SYNC_INTERVAL}"
done
