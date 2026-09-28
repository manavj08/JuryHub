# Scoreline: one small image, one process, SQLite on a volume.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PORT=8080 \
    SCORELINE_DB=/data/scoreline.db \
    SCORELINE_FIXTURES=/app/data/fixtures.json

WORKDIR /app

# Wheels for x86_64 and arm64 are vendored, so the image builds with the
# network off as long as the python:3.12-slim base image is already pulled.
COPY requirements.txt ./
COPY vendor/wheels ./vendor/wheels
RUN pip install --no-cache-dir --no-index --find-links ./vendor/wheels -r requirements.txt \
 || pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY data/fixtures.json ./data/fixtures.json

RUN useradd --create-home --uid 10001 scoreline \
 && mkdir -p /data \
 && chown scoreline /data
USER scoreline
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8080'), timeout=2)"

# Seed first (idempotent), then serve. One worker: the rate limiter is in
# memory and SQLite prefers a single writer process.
CMD ["sh", "-c", "python -m scoreline.seed && exec uvicorn scoreline.main:app --host 0.0.0.0 --port ${PORT}"]
