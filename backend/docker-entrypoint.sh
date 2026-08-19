#!/bin/sh
# Wait for PostgreSQL, apply migrations, then hand over to the server.
#
# Migrations run here rather than in the application so a rolling restart does not
# have several workers racing to migrate the same database.
set -e

echo "waiting for the database…"
python - <<'PY'
import os
import sys
import time
from urllib.parse import urlparse

url = urlparse(os.environ.get("DATABASE_URL", ""))
if url.scheme.startswith("sqlite") or not url.hostname:
    sys.exit(0)

import socket

deadline = time.time() + 60
while time.time() < deadline:
    try:
        with socket.create_connection((url.hostname, url.port or 5432), timeout=3):
            sys.exit(0)
    except OSError:
        time.sleep(1)

print("database did not become reachable within 60 seconds", file=sys.stderr)
sys.exit(1)
PY

echo "applying database migrations…"
alembic upgrade head

echo "starting: $*"
exec "$@"
