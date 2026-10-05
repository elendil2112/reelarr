FROM python:3.12-slim

LABEL org.opencontainers.image.title="Reelarr" \
      org.opencontainers.image.description="Identify, tag and file live concert recordings" \
      org.opencontainers.image.licenses="GPL-3.0"

# Folders are NOT set here: the setup wizard picks them, from whatever you
# mount (by default /media). PUID/PGID/UMASK/TZ are read by the entrypoint.
ENV PYTHONUNBUFFERED=1 \
    REELARR_CONFIG=/config \
    REELARR_HOST=0.0.0.0 \
    REELARR_PORT=8189 \
    PUID=1000 PGID=1000 UMASK=022 TZ=Etc/UTC

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates tzdata p7zip-full \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY app /app/app
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh && mkdir -p /config /media
WORKDIR /app

VOLUME ["/config"]
EXPOSE 8189
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8189/api/health', timeout=4)" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
