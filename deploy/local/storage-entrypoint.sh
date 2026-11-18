#!/bin/bash
# The storage's two processes: the encrypted database (started as root only
# to mount the encrypted directory; Postgres itself runs as postgres) and
# the memory's API (uid 10001). When either stops, the container stops, and
# the restart policy brings both back together.
set -uo pipefail
export BRAIN_LISTEN=127.0.0.1
/usr/local/bin/db-entrypoint.sh &
DB=$!
# A STOP (2026-11-18): the API first, so nothing writes, then Postgres's
# FAST shutdown (SIGINT: the clients are cut, a shutdown checkpoint is
# written) — $DB is Postgres itself since db-entrypoint execs into it.
trap 'kill -TERM ${API:+"$API"} 2>/dev/null; kill -INT "$DB" 2>/dev/null' TERM INT
echo "storage: waiting for the database (the first start creates it, which takes minutes)"
until pg_isready -q -h 127.0.0.1 -U "${POSTGRES_USER:-brain}" -d postgres; do
  kill -0 "$DB" 2>/dev/null || { echo "storage: the database stopped before it was ready" >&2; exit 1; }
  sleep 2
done
echo "storage: database ready — starting the API"
setpriv --reuid 10001 --regid 10001 --clear-groups \
  uvicorn api.main:app --host 0.0.0.0 --port 8610 &
API=$!
wait -n "$DB" "$API"
code=$?
kill -TERM "$API" 2>/dev/null
kill -INT "$DB" 2>/dev/null
wait
exit "$code"
