#!/bin/sh
# Start as root just long enough to make /config writable, then drop to PUID:PGID
# (99:100 is Unraid's nobody:users, which also owns the media shares).
set -e

mkdir -p "$DATA_DIR"
if [ "$(id -u)" = "0" ]; then
    if [ "$(stat -c %u:%g "$DATA_DIR")" != "$PUID:$PGID" ]; then
        chown -R "$PUID:$PGID" "$DATA_DIR"
    fi
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi
exec "$@"
