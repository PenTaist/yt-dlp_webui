FROM python:3.12-slim

RUN useradd -u 1000 -m app \
 && mkdir -p /opt/yt-dlp/bin \
 && chown -R app:app /opt/yt-dlp

COPY updater.py /usr/local/bin/updater.py

USER app
ENV PYTHONUNBUFFERED=1
VOLUME ["/opt/yt-dlp"]
CMD ["python3", "/usr/local/bin/updater.py"]
