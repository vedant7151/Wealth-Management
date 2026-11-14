#!/bin/sh
# Waits briefly for DATABASE_URL to accept connections (helps the local
# docker-compose profile's race between the `migrate` one-shot service
# and this container both starting at once, and tolerates a cold-starting
# managed Postgres like Neon), then execs uvicorn with worker count and
# graceful-shutdown timeout from env so a plain `docker run -e ...` can
# tune them without rebuilding the image. Never runs a schema migration
# itself -- that is ALWAYS a separate, explicit step (see
# docker-compose.yml's `migrate` service and docs/DEPLOY.md), never
# something a container does automatically on every start.
set -e

# Fail fast on bad/missing configuration (prints variable NAMES only, never
# values; exits 78 = EX_CONFIG). The same validation runs again inside the
# app, but doing it here gives a one-line failure before any waiting.
python settings.py

if [ -n "${DATABASE_URL:-}" ]; then
    echo "Waiting for the database to accept connections..."
    python - <<'PY'
import os
import sys
import time

try:
    import psycopg2
except ImportError:
    sys.exit(0)

url = os.environ.get("DATABASE_URL", "")
for attempt in range(1, 16):
    try:
        conn = psycopg2.connect(url, connect_timeout=5)
        conn.close()
        print("Database is reachable.")
        sys.exit(0)
    except Exception as e:  # noqa: BLE001
        print(f"Database not ready yet (attempt {attempt}/15): {e}")
        time.sleep(2)
print("Database still unreachable after retries -- starting anyway; requests that need it will fail until it's up.")
PY
fi

exec python -m uvicorn api:app \
    --host 0.0.0.0 \
    --port "${PORT:-8010}" \
    --workers "${WEB_CONCURRENCY:-1}" \
    --timeout-graceful-shutdown "${GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS:-25}"
