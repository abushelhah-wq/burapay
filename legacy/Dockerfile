# Runtime image for the busrapay benchmark web app.
#
# TLS is NOT terminated here — deploy/docker-compose.yml puts Caddy in front, which
# obtains and renews a real certificate automatically. That is deliberate: a real
# certificate is a hard requirement for hosted checkout, because gateway sandboxes
# reject return and callback URLs they cannot verify, and a self-signed cert is
# exactly that.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Dependencies first, so a code change does not invalidate the dependency layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY serve.py ./

# The app runs unprivileged and owns only its data directory.
RUN useradd --system --create-home --uid 10001 busrapay \
 && mkdir -p /srv/data \
 && chown -R busrapay:busrapay /srv/data
USER busrapay

ENV HOST=0.0.0.0 \
    PORT=8000 \
    BEHIND_PROXY=1 \
    DATABASE_PATH=/srv/data/results.db

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)"

CMD ["python", "serve.py"]
