#!/bin/sh
# Make the state dir writable by the non-root app user, then drop privileges.
#
# On a bind-mounted ./data (the default in docker-compose.yml), the host directory's
# ownership wins over the image's — so a fresh, root-owned ./data on a new VPS would be
# unwritable by the uid-10001 app user, and SQLite would fail to open its database.
# Running as root only long enough to chown fixes that for any host setup, then we exec
# the app as the unprivileged user.
set -e
# Render Secret File (Dashboard → Environment → Secret Files named config.yaml)
# lands at /etc/secrets/config.yaml. Copy it to the path the image CMD uses.
if [ -f /etc/secrets/config.yaml ]; then
  cp /etc/secrets/config.yaml /app/config.yaml
  chown appuser:appuser /app/config.yaml 2>/dev/null || true
fi
chown -R appuser:appuser /app/data 2>/dev/null || true
exec gosu appuser "$@"
