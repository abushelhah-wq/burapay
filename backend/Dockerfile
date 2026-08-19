# BuraPay backend.
#
# Runs migrations at start-up and then serves the API with uvicorn on port 8000. TLS
# is not terminated here and no port is published: the VPS's Traefik holds the
# certificate and is the only route in.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# Dependencies first, so a code change does not invalidate the dependency layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

# Unprivileged: the process needs no write access to anything but /tmp.
RUN useradd --system --create-home --uid 10001 burapay \
 && chown -R burapay:burapay /srv
USER burapay

EXPOSE 8000

# The health check exercises the real endpoint, which touches the database — a
# process that is up but cannot reach PostgreSQL is not healthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD python -c "import sys, urllib.request; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
