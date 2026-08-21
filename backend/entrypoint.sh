#!/bin/sh
# Container entrypoint.
#
# `api` runs migrations and then serves. Running `alembic upgrade head` here is
# what makes §7's deploy sequence hold: `docker compose up -d` brings the schema
# current with no manual step and no DB surgery on the VPS.
#
# Migrations are idempotent, so the worker replica starting at the same time is
# harmless -- but only the api role runs them, so there is no race to begin with.
set -e

case "${1:-api}" in
  api)
    echo '{"level":"INFO","logger":"entrypoint","message":"running database migrations"}'
    alembic upgrade head
    echo '{"level":"INFO","logger":"entrypoint","message":"migrations complete; starting api"}'
    exec uvicorn app.main:app \
      --host 0.0.0.0 --port 8000 \
      --proxy-headers --forwarded-allow-ips='*' \
      --log-level "$(echo "${LOG_LEVEL:-info}" | tr '[:upper:]' '[:lower:]')"
    ;;

  worker)
    # Background jobs: webhook delivery retries and scheduled benchmark runs.
    # Nothing in the request path depends on this being up (§1) -- a payment
    # never waits on the queue.
    echo '{"level":"INFO","logger":"entrypoint","message":"starting rq worker"}'
    exec rq worker --url "${REDIS_URL:-redis://redis:6379/0}" burapay
    ;;

  migrate)
    exec alembic upgrade head
    ;;

  *)
    exec "$@"
    ;;
esac
