FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 cuesync \
 && mkdir /data \
 && chown cuesync:cuesync /data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV CUESYNC_HOST=0.0.0.0 \
    CUESYNC_PORT=7000 \
    CUESYNC_DATA_DIR=/data \
    PYTHONUNBUFFERED=1

VOLUME /data
EXPOSE 7000
USER cuesync

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7000/health', timeout=4)"

CMD ["cuesync"]
