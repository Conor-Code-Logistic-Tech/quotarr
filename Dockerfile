FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY quotarr ./quotarr
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV DATA_DIR=/config \
    PUID=99 \
    PGID=100 \
    XDG_CACHE_HOME=/tmp/.cache \
    PYTHONUNBUFFERED=1

EXPOSE 6464
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:6464/healthz', timeout=4)"

ENTRYPOINT ["docker-entrypoint.sh"]
# One worker on purpose: the indexer runs inside the app process.
CMD ["uvicorn", "quotarr.main:app", "--host", "0.0.0.0", "--port", "6464", "--proxy-headers", "--forwarded-allow-ips", "*"]
