#!/bin/sh
# Reelarr container entrypoint.
#
#   PUID / PGID   who owns the files Reelarr creates (default 1000:1000;
#                 unRAID: 99:100). Set these in Docker — a running container
#                 can't change who it runs as.
#   UMASK         permission mask for new files (default 022; unRAID users
#                 often want 000 so SMB shares can edit everything)
#   TZ            time zone for logs (the schedule's zone is set in the app)
#
# Runs as root only long enough to create the user and fix /config ownership,
# then drops to PUID:PGID for the app itself.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
UMASK="${UMASK:-022}"

case "$PUID$PGID" in
    *[!0-9]*) echo "[reelarr] PUID and PGID must be numbers (got PUID=$PUID PGID=$PGID)" >&2; exit 1 ;;
esac

umask "$UMASK"
mkdir -p /config

if [ "$(id -u)" != "0" ]; then
    # started as a non-root user already (docker run --user …): just run
    exec python -m app
fi

if [ "$PUID" = "0" ]; then
    echo "[reelarr] running as root (PUID=0) — files will be owned by root" >&2
    exec python -m app
fi

if ! getent group "$PGID" >/dev/null 2>&1; then
    groupadd -o -g "$PGID" reelarr
fi
if ! getent passwd "$PUID" >/dev/null 2>&1; then
    useradd -o -u "$PUID" -g "$PGID" -d /config -M -s /usr/sbin/nologin reelarr
fi

# only touch what isn't already right — a big /config shouldn't slow every start
find /config \( ! -user "$PUID" -o ! -group "$PGID" \) -exec chown "$PUID:$PGID" {} + 2>/dev/null || true

echo "[reelarr] starting as uid=$PUID gid=$PGID umask=$UMASK" >&2
export HOME=/config
exec setpriv --reuid="$PUID" --regid="$PGID" --init-groups python -m app
