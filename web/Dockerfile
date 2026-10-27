ARG DENO_IMAGE=denoland/deno:bin
FROM ${DENO_IMAGE} AS deno

FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=deno /deno /usr/local/bin/deno

RUN useradd -u 1000 -m app \
 && mkdir -p /downloads /config \
 && chown app:app /downloads

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY static ./static

USER app
ENV PYTHONUNBUFFERED=1 \
    DENO_DIR=/tmp/deno
EXPOSE 8080
VOLUME ["/downloads"]

CMD ["gunicorn", "--workers", "1", "--threads", "16", "--bind", "0.0.0.0:8080", \
     "--timeout", "300", "--access-logfile", "-", "app:app"]
