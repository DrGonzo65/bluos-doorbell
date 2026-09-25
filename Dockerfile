FROM python:3.12-slim

# Stamped by CI so /health can tell you exactly which build is running —
# the thing you actually want to check after clicking Apply Update.
ARG GIT_SHA=dev
ARG BUILD_TIME=unknown

LABEL org.opencontainers.image.title="BluOS Doorbell" \
      org.opencontainers.image.description="Doorbell chime for Bluesound players with correct volume and source restore" \
      org.opencontainers.image.revision="${GIT_SHA}"

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY tools/ ./tools/

# Seeded into the mounted volumes on first run, so a GUI-only install
# (Unraid template / Community Apps) never needs shell access.
COPY chimes/ ./defaults/chimes/

ENV DOORBELL_DEFAULTS=/srv/defaults \
    DOORBELL_CONFIG=/config/config.yaml \
    DOORBELL_CHIME_DIR=/chimes \
    DOORBELL_GIT_SHA=${GIT_SHA} \
    DOORBELL_BUILD_TIME=${BUILD_TIME} \
    PYTHONUNBUFFERED=1

# The listen port comes from config.yaml, not from here.
EXPOSE 8095

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; \
from app.config import load_config; p=load_config().listen_port; \
urllib.request.urlopen(f'http://127.0.0.1:{p}/health', timeout=4)" || exit 1

CMD ["python", "-m", "app"]
