# KNAVIS backend. One process only: local on-disk Qdrant and the in-process rate limiter belong to a single worker.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

WORKDIR /srv/backend
COPY requirements.txt .
# pytest is only for the test suite; keep it out of the runtime image.
RUN grep -v -i '^pytest' requirements.txt > runtime-requirements.txt \
    && pip install -r runtime-requirements.txt \
    && rm runtime-requirements.txt

COPY app ./app

# Run as an unprivileged user that owns the data volume.
RUN useradd --system --uid 10001 --home-dir /srv knavis \
    && mkdir -p /data \
    && chown -R knavis /data
USER knavis
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health/live', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
