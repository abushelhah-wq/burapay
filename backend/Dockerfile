# Backend API container.
#
# Serves JSON only. The frontend is a separate service (§1) -- nothing here
# renders HTML for the application.

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/srv

WORKDIR /srv

# Dependencies first, so a code change does not invalidate the dependency layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

# Runs unprivileged. It owns no writable directory -- all state is in Postgres,
# so there is nothing on this filesystem worth granting write access to.
RUN useradd --system --create-home --uid 10001 burapay
USER burapay

EXPOSE 8000

# Checks the database only, never a payment gateway (§6).
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

ENTRYPOINT ["./entrypoint.sh"]
CMD ["api"]
